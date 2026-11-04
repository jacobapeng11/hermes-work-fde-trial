from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.hermes_runtime.constants import (
    HERMES_HOME,
    HERMES_WORKSPACE,
    RUNTIME_LOCAL_MODEL_RELAY_BASE_URL,
)
from app.hermes_runtime.projection import ProjectionInput
from app.hermes_runtime.skill_visibility_policy import (
    INITIAL_DEFAULT_SKILL_POLICY_VERSION,
    SKILL_POLICY_CONFIG_KEY,
    SKILL_VISIBILITY_POLICY_V1,
    SKILL_VISIBILITY_POLICY_V2,
    SKILL_VISIBILITY_POLICY_V3,
)
from app.hermes_runtime.projection import HermesWorkspaceProjector
from app.hermes_runtime.providers.knative import KnativeRuntimeSpec
from app.models import CloudHermesProfile, HermesRuntimeBinding, User
from app.models.runtime import PersonalConfigBundleArtifact
from app.services.runtime_config_bundle import (
    config_bundle_digest,
    personal_config_bundle_files,
)
from app.services.runtime_agent_profiles import (
    SqlAlchemyRuntimeAgentProfileStore,
)
from app.services.runtime_models import SqlAlchemyRuntimeModelStore


class PersonalConfigBundleError(RuntimeError):
    pass


BOOTSTRAP_CONFIG_SCHEMA = 34
BOOTSTRAP_CONFIG_REVISION_PREFIX = "bootstrap-generation-"
LEGACY_CONFIG_REVISION_PREFIX = "generation-"


def legacy_config_revision(generation: int) -> str:
    return f"{LEGACY_CONFIG_REVISION_PREFIX}{generation}"


def bootstrap_config_revision(generation: int) -> str:
    return f"{BOOTSTRAP_CONFIG_REVISION_PREFIX}{generation}"


def is_bootstrap_config_revision(revision: object) -> bool:
    return str(revision or "").startswith(BOOTSTRAP_CONFIG_REVISION_PREFIX)


@dataclass(frozen=True)
class PersonalConfigBundle:
    profile_id: int
    generation: int
    config_revision: str
    config_digest: str
    files: dict[str, str]


class PersonalConfigBundleBuilder:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def find_artifact(
        self,
        *,
        profile_id: int,
        generation: int,
    ) -> PersonalConfigBundleArtifact | None:
        return await self.db.scalar(
            select(PersonalConfigBundleArtifact).where(
                PersonalConfigBundleArtifact.runtime_workspace_id == profile_id,
                PersonalConfigBundleArtifact.config_generation == generation,
            )
        )

    async def record_artifact(
        self,
        bundle: PersonalConfigBundle,
        *,
        org_id: int,
    ) -> PersonalConfigBundleArtifact:
        """以 exact-generation 工件固化 Bundle；同代数不同摘要必须失败。"""
        existing = await self.find_artifact(
            profile_id=bundle.profile_id,
            generation=bundle.generation,
        )
        if existing is not None:
            if existing.config_digest != bundle.config_digest:
                raise PersonalConfigBundleError(
                    "Runtime config artifact digest conflict"
                )
            return existing
        artifact = PersonalConfigBundleArtifact(
            org_id=org_id,
            runtime_workspace_id=bundle.profile_id,
            config_generation=bundle.generation,
            config_revision=bundle.config_revision,
            config_digest=bundle.config_digest,
            files=bundle.files,
        )
        self.db.add(artifact)
        try:
            await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            raise PersonalConfigBundleError(
                "Runtime config artifact digest conflict"
            ) from exc
        return artifact

    async def build(
        self,
        *,
        profile_id: int,
        generation: int,
        require_current: bool = False,
        config_schema: int = 33,
        bootstrap: bool = False,
        reuse_artifact: bool = True,
    ) -> PersonalConfigBundle:
        if generation < 0:
            raise PersonalConfigBundleError(
                "Runtime config generation is invalid"
            )
        artifact = None
        if reuse_artifact:
            artifact = await self.find_artifact(
                profile_id=profile_id,
                generation=generation,
            )
        if artifact is not None:
            # Exact-generation 工件不可变：实时身份/目录变化不得改写历史摘要；
            # 变化必须通过新的 config generation 产生新工件。
            return PersonalConfigBundle(
                profile_id=profile_id,
                generation=generation,
                config_revision=str(artifact.config_revision),
                config_digest=str(artifact.config_digest),
                files=dict(artifact.files or {}),
            )
        profile = await self.db.scalar(
            select(CloudHermesProfile).where(
                CloudHermesProfile.id == profile_id,
                CloudHermesProfile.status == "active",
            )
        )
        if profile is None:
            raise PersonalConfigBundleError("Runtime profile is not active")
        binding = await self.db.scalar(
            select(HermesRuntimeBinding).where(
                HermesRuntimeBinding.runtime_workspace_id == profile_id,
            )
        )
        if require_current and (
            binding is None
            or int(binding.config_generation or 0) != generation
        ):
            raise PersonalConfigBundleError(
                "Runtime config revision is no longer current"
            )
        default_agent_profile = await SqlAlchemyRuntimeAgentProfileStore(
            self.db
        ).ensure_default(workspace=profile)
        visible_models = ()
        if not bootstrap:
            visible_models = tuple(
                await SqlAlchemyRuntimeModelStore(
                    self.db
                ).list_visible_models(
                    org_id=int(profile.org_id),
                    user_id=int(profile.user_id),
                )
            )
        user = await self.db.scalar(
            select(User).where(
                User.id == profile.user_id,
                User.org_id == profile.org_id,
                User.status == "active",
            )
        )
        if user is None or not user.user_code:
            raise PersonalConfigBundleError(
                "Runtime user identity is unavailable"
            )
        # The initial Runtime bundle is intentionally asset-free. Stable
        # Profile Desired revisions are the sole authority for Skills, MCPs,
        # and knowledge; ProfileConvergenceWorker applies them once the fresh
        # Runtime is ready. This keeps Runtime bootstrap independent from the
        # retired draft/generation and workspace-level binding tables.
        profile_config = dict(getattr(profile, "profile_config", None) or {})
        desired_policy = profile_config.get(SKILL_POLICY_CONFIG_KEY)
        if desired_policy not in {
            SKILL_VISIBILITY_POLICY_V1,
            SKILL_VISIBILITY_POLICY_V2,
            SKILL_VISIBILITY_POLICY_V3,
        }:
            desired_policy = None
        projection_input = ProjectionInput(
            org_id=int(profile.org_id),
            user_id=int(profile.user_id),
            runtime_workspace_id=int(profile.id),
            runtime_id=(
                str(binding.runtime_id)
                if binding is not None and binding.runtime_id
                else f"rt-{int(profile.id)}"
            ),
            config_generation=generation,
            model_relay_base_url=RUNTIME_LOCAL_MODEL_RELAY_BASE_URL,
            hermes_home=Path(HERMES_HOME),
            workspace_dir=Path(HERMES_WORKSPACE),
            name=str(profile.name),
            runtime_agent_profile_id=int(default_agent_profile.id),
            default_model_id=(
                None
                if bootstrap
                else (
                    int(profile.default_model_id)
                    if profile.default_model_id is not None
                    else None
                )
            ),
            visible_models=(
                ()
                if bootstrap
                else tuple(
                    {
                        "model_entry_id": model.model_entry_id,
                        "context_length": model.context_length,
                        "tags": list(model.tags or []),
                        "is_default": bool(
                            getattr(model, "is_default", False)
                        ),
                    }
                    for model in visible_models
                )
            ),
            config_schema=config_schema,
            skill_policy_version=(
                desired_policy
                if desired_policy is not None
                else (INITIAL_DEFAULT_SKILL_POLICY_VERSION if generation == 0 else None)
            ),
            bootstrap_config=bootstrap,
        )
        files = personal_config_bundle_files(
            artifacts=HermesWorkspaceProjector().render_artifacts(
                projection_input
            ),
            name=str(user.name),
            user_code=str(user.user_code),
        )
        return PersonalConfigBundle(
            profile_id=profile_id,
            generation=generation,
            config_revision=(
                bootstrap_config_revision(generation)
                if bootstrap
                else legacy_config_revision(generation)
            ),
            config_digest=config_bundle_digest(files),
            files=files,
        )


class PersonalRuntimeInitialConfigPreparer:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.builder = PersonalConfigBundleBuilder(db)

    async def prepare(
        self,
        spec: KnativeRuntimeSpec,
    ) -> KnativeRuntimeSpec:
        binding = await self.db.scalar(
            select(HermesRuntimeBinding).where(
                HermesRuntimeBinding.runtime_workspace_id
                == spec.runtime_workspace_id,
            ).with_for_update()
        )
        if binding is None:
            raise PersonalConfigBundleError("Runtime binding is unavailable")

        bootstrap = spec.config_schema >= BOOTSTRAP_CONFIG_SCHEMA
        generation = int(binding.config_generation or 0)
        # 兑现 SSO 在 in-flight（starting/updating）期间记录的 identity 意图：
        # 推进新代数后以最新身份构建工件，避免复用旧身份的 exact-generation
        # 工件导致漂移永不收敛。
        identity_pending = "identity" in set(binding.pending_reload_scopes or [])
        if bootstrap and (
            not is_bootstrap_config_revision(binding.config_hash)
            or identity_pending
        ):
            generation += 1
            binding.config_generation = generation
            binding.config_hash = bootstrap_config_revision(generation)
            if identity_pending:
                scopes = set(binding.pending_reload_scopes or [])
                scopes.discard("identity")
                binding.pending_reload_scopes = sorted(scopes)
            await self.db.commit()
        elif bootstrap:
            # Exact-generation 工件不可变，但渲染契约本身可能升级（例如新的
            # 平台 Skill root）。当同代数工件的摘要与当前渲染不一致时，按
            # 合同推进 generation 产生新工件，而不是让旧配置无限复用。
            existing = await self.builder.build(
                profile_id=spec.runtime_workspace_id,
                generation=generation,
                config_schema=spec.config_schema,
                bootstrap=True,
            )
            candidate = await self.builder.build(
                profile_id=spec.runtime_workspace_id,
                generation=generation,
                config_schema=spec.config_schema,
                bootstrap=True,
                reuse_artifact=False,
            )
            if existing.config_digest != candidate.config_digest:
                generation += 1
                binding.config_generation = generation
            binding.config_hash = bootstrap_config_revision(generation)
            await self.db.commit()
        elif is_bootstrap_config_revision(binding.config_hash):
            binding.config_hash = legacy_config_revision(generation)
            await self.db.commit()

        bundle = await self.builder.build(
            profile_id=spec.runtime_workspace_id,
            generation=generation,
            require_current=False,
            config_schema=spec.config_schema,
            bootstrap=bootstrap,
        )
        await self.builder.record_artifact(
            bundle,
            org_id=int(binding.org_id),
        )
        await self.db.commit()
        return replace(
            spec,
            runtime_config_revision=bundle.config_revision,
            initial_config_download_url=(
                "/internal/v1/config-bundles/personal/"
                f"{spec.runtime_workspace_id}/{generation}"
            ),
            initial_config_digest=bundle.config_digest,
        )
