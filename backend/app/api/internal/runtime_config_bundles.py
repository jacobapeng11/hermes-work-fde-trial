from __future__ import annotations

import hmac
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    ApplicationAgent,
    ApplicationAgentConfigRevision,
    ApplicationAgentRuntimeBinding,
    HermesRuntimeBinding,
    User,
)
from app.schemas.runtime_internal import ConfigBundlePayload
from app.services.personal_config_bundle import (
    BOOTSTRAP_CONFIG_SCHEMA,
    PersonalConfigBundleBuilder,
    PersonalConfigBundleError,
    bootstrap_config_revision,
    legacy_config_revision,
)
from app.services.application_agent_runtime import (
    ApplicationAgentCapabilityResolver,
    ApplicationAgentConfigArtifactStore,
    ApplicationAgentConfigBootstrapper,
    ApplicationAgentRuntimeError,
    SqlAlchemyApplicationAgentRuntimeManager,
)
from app.services.runtime_image_contract import (
    resolve_runtime_image_contract,
)
from app.services.runtime_config_bootstrap import (
    LocalRuntimeConfigBootstrapper,
)
from app.services.runtime_models import SqlAlchemyRuntimeModelStore
from app.services.runtime_projection import (
    RuntimeProjectionAuthenticationError,
    RuntimeProjectionBindingError,
    RuntimeProjectionService,
)


router = APIRouter(tags=["internal-runtime-config-bundles"])


def _application_revision_agent(
    agent: ApplicationAgent,
    revision: ApplicationAgentConfigRevision,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=int(agent.id),
        org_id=int(agent.org_id),
        created_by_user_id=int(agent.created_by_user_id),
        slug=str(agent.slug),
        config_generation=int(revision.generation),
        config=dict(revision.config_snapshot or {}),
    )


def _bearer_token(authorization: str | None) -> str:
    scheme, _, token = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runtime config authorization required",
        )
    return token


def _numeric_binding_id(binding_id: str) -> int:
    try:
        parsed = int(binding_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Runtime config binding is stale",
        ) from exc
    if parsed <= 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Runtime config binding is stale",
        )
    return parsed


@router.get(
    "/v1/config-bundles/personal/{profile_id}/{generation}",
    response_model=ConfigBundlePayload,
)
async def download_personal_config_bundle(
    profile_id: int,
    generation: int,
    authorization: str | None = Header(default=None),
    runtime_id: str = Header(alias="X-Hermes-Runtime-Id"),
    binding_id: str = Header(alias="X-Hermes-Binding-Id"),
    config_revision: str = Header(alias="X-Hermes-Config-Revision"),
    expected_digest: str = Header(alias="X-Hermes-Config-Digest"),
    db: AsyncSession = Depends(get_db),
) -> ConfigBundlePayload:
    # Retry logic for transient connection errors
    max_retries = 2
    for attempt in range(max_retries):
        try:
            return await _download_personal_config_bundle_impl(
                profile_id=profile_id,
                generation=generation,
                authorization=authorization,
                runtime_id=runtime_id,
                binding_id=binding_id,
                config_revision=config_revision,
                expected_digest=expected_digest,
                db=db,
            )
        except Exception as exc:
            error_message = str(exc)
            if ("Packet sequence number wrong" in error_message or "PendingRollbackError" in error_message) and attempt < max_retries - 1:
                # Roll back this request's transaction, then retry once.
                try:
                    await db.rollback()
                except Exception:
                    pass
                continue
            raise


async def _download_personal_config_bundle_impl(
    profile_id: int,
    generation: int,
    authorization: str | None,
    runtime_id: str,
    binding_id: str,
    config_revision: str,
    expected_digest: str,
    db: AsyncSession,
) -> ConfigBundlePayload:
    try:
        await RuntimeProjectionService(db).authenticate(
            runtime_id=runtime_id,
            binding_id=binding_id,
            api_server_key=_bearer_token(authorization),
        )
    except RuntimeProjectionAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runtime config authorization failed",
        ) from exc
    except RuntimeProjectionBindingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Runtime config binding is stale",
        ) from exc

    binding = await db.scalar(
        select(HermesRuntimeBinding).where(
            HermesRuntimeBinding.id == _numeric_binding_id(binding_id),
            HermesRuntimeBinding.runtime_workspace_id == profile_id,
            HermesRuntimeBinding.runtime_id == runtime_id,
        )
    )
    if binding is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Runtime config target not found",
        )
    legacy_revision = legacy_config_revision(generation)
    bootstrap_revision = bootstrap_config_revision(generation)
    bootstrap = config_revision == bootstrap_revision
    if config_revision != legacy_revision and not bootstrap:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Runtime config revision is stale",
        )
    contract = resolve_runtime_image_contract(
        repository_root=Path(__file__).resolve().parents[4],
        image_reference=str(binding.image),
    )
    if bootstrap and contract.config_schema < BOOTSTRAP_CONFIG_SCHEMA:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Runtime config revision is stale",
        )
    try:
        bundle = await PersonalConfigBundleBuilder(db).build(
            profile_id=profile_id,
            generation=generation,
            require_current=True,
            config_schema=contract.config_schema,
            bootstrap=bootstrap,
        )
    except PersonalConfigBundleError as exc:
        if int(binding.config_generation or 0) != generation:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Runtime config revision is no longer current",
            ) from exc
        try:
            bundle = await PersonalConfigBundleBuilder(db).build(
                profile_id=profile_id,
                generation=generation,
                require_current=False,
                config_schema=contract.config_schema,
                bootstrap=bootstrap,
            )
        except PersonalConfigBundleError as fallback_exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Runtime config revision is unavailable",
            ) from fallback_exc
    files = bundle.files
    observed_digest = bundle.config_digest
    if not hmac.compare_digest(observed_digest, expected_digest):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Runtime config digest changed",
        )
    return ConfigBundlePayload(
        runtime_id=runtime_id,
        binding_id=binding_id,
        config_revision=(
            bootstrap_revision if bootstrap else legacy_revision
        ),
        config_digest=observed_digest,
        files=files,
    )


@router.get(
    "/v1/config-bundles/application/{agent_id}/{generation}",
    response_model=ConfigBundlePayload,
)
async def download_application_config_bundle(
    agent_id: int,
    generation: int,
    baseline: int = Query(default=1, ge=1),
    authorization: str | None = Header(default=None),
    runtime_id: str = Header(alias="X-Hermes-Runtime-Id"),
    binding_id: str = Header(alias="X-Hermes-Binding-Id"),
    config_revision: str = Header(alias="X-Hermes-Config-Revision"),
    expected_digest: str = Header(alias="X-Hermes-Config-Digest"),
    db: AsyncSession = Depends(get_db),
) -> ConfigBundlePayload:
    try:
        await RuntimeProjectionService(db).authenticate(
            runtime_id=runtime_id,
            binding_id=binding_id,
            api_server_key=_bearer_token(authorization),
        )
    except RuntimeProjectionAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runtime config authorization failed",
        ) from exc
    except RuntimeProjectionBindingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Runtime config binding is stale",
        ) from exc

    row = (
        await db.execute(
            select(
                ApplicationAgent,
                ApplicationAgentRuntimeBinding,
                ApplicationAgentConfigRevision,
                User,
            )
            .join(
                ApplicationAgentRuntimeBinding,
                ApplicationAgentRuntimeBinding.application_agent_id
                == ApplicationAgent.id,
            )
            .join(User, User.id == ApplicationAgent.created_by_user_id)
            .join(
                ApplicationAgentConfigRevision,
                ApplicationAgentConfigRevision.application_agent_id
                == ApplicationAgent.id,
            )
            .where(
                ApplicationAgent.id == agent_id,
                ApplicationAgentRuntimeBinding.id
                == _numeric_binding_id(binding_id),
                ApplicationAgentRuntimeBinding.runtime_id == runtime_id,
                ApplicationAgentConfigRevision.generation == generation,
                User.org_id == ApplicationAgent.org_id,
                User.status == "active",
            )
        )
    ).first()
    revision = f"generation-{generation}"
    if row is None or config_revision != revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Runtime config revision is no longer current",
        )
    agent, binding, config_snapshot, creator = row
    revision_agent = _application_revision_agent(
        agent,
        config_snapshot,
    )
    try:
        files, observed_digest = ApplicationAgentConfigArtifactStore.read(
            runtime_root_host_path=str(binding.runtime_root_host_path),
            generation=generation,
            baseline_epoch=baseline,
        )
    except FileNotFoundError:
        # Compatibility for generations created before immutable artifacts
        # were introduced. New generations always use the exact artifact.
        contract = resolve_runtime_image_contract(
            repository_root=Path(__file__).resolve().parents[4],
            image_reference=str(binding.image),
        )
        spec = SqlAlchemyApplicationAgentRuntimeManager._build_runtime_spec(
            agent=revision_agent,
            creator=creator,
            binding=binding,
            upstream_commit=contract.upstream_commit,
            state_schema=contract.state_schema,
            config_schema=contract.config_schema,
        )
        capabilities = await ApplicationAgentCapabilityResolver(db).resolve(
            agent=revision_agent
        )
        files, observed_digest = await ApplicationAgentConfigBootstrapper(
            LocalRuntimeConfigBootstrapper(
                model_store=SqlAlchemyRuntimeModelStore(db),
                kanban_orchestrator_enabled=False,
            )
        ).render_bundle(
            spec,
            agent=revision_agent,
            capabilities=capabilities,
        )
    except ApplicationAgentRuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.code,
        ) from exc
    if not hmac.compare_digest(observed_digest, expected_digest):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Runtime config digest changed",
        )
    return ConfigBundlePayload(
        runtime_id=runtime_id,
        binding_id=binding_id,
        config_revision=revision,
        config_digest=observed_digest,
        files=files,
    )
