"""Pure read-only analysis for the Skill/Expert one-time semantic cutover."""

from __future__ import annotations

from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileFact(_Fact):
    profile_id: int = Field(gt=0)
    runtime_workspace_id: int = Field(gt=0)
    profile_kind: str
    ownership_type: str
    status: str
    display_name_key: str
    desired_revision: int = Field(ge=0)
    applied_revision: int = Field(ge=0)


class ExpertPackageFact(_Fact):
    package_id: int = Field(gt=0)
    org_id: int = Field(gt=0)
    status: str
    execution_status: str
    market_name_key: str | None = None
    current_version_id: int | None = Field(default=None, gt=0)


class ExpertVersionFact(_Fact):
    version_id: int = Field(gt=0)
    package_id: int = Field(gt=0)
    status: str
    skill_ids: list[int] = Field(default_factory=list)
    unresolved_skill_refs: list[str] = Field(default_factory=list)


class InstallationFact(_Fact):
    binding_id: int = Field(gt=0)
    runtime_workspace_id: int = Field(gt=0)
    package_id: int = Field(gt=0)
    version_id: int = Field(gt=0)
    profile_id: int = Field(gt=0)
    status: str
    installation_epoch: int = Field(ge=0)


class SkillBindingFact(_Fact):
    binding_id: int = Field(gt=0)
    profile_id: int = Field(gt=0)
    skill_id: int = Field(gt=0)
    skill_version_id: int | None = Field(default=None, gt=0)
    version_policy: str
    status: str


class StableAssetFact(_Fact):
    asset_id: int = Field(gt=0)
    current_version_id: int | None = Field(default=None, gt=0)


class RolloutFact(_Fact):
    rollout_id: str
    status: str


class SkillExpertPreflightSnapshot(_Fact):
    profiles: list[ProfileFact] = Field(default_factory=list)
    packages: list[ExpertPackageFact] = Field(default_factory=list)
    versions: list[ExpertVersionFact] = Field(default_factory=list)
    installations: list[InstallationFact] = Field(default_factory=list)
    skill_bindings: list[SkillBindingFact] = Field(default_factory=list)
    skills: list[StableAssetFact] = Field(default_factory=list)
    skill_versions: list[int] = Field(default_factory=list)
    incomplete_rollouts: list[RolloutFact] = Field(default_factory=list)


class PreflightIssue(_Fact):
    severity: Literal["blocking", "migration_required", "warning"]
    code: str
    entity_type: str
    entity_id: str
    related_ids: list[str] = Field(default_factory=list)


class SkillExpertPreflightReport(_Fact):
    schema_version: Literal[1] = 1
    mode: Literal["dry-run"] = "dry-run"
    status: Literal["ready", "blocked"]
    counts: dict[str, int]
    issues: list[PreflightIssue]


def _normalized_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def analyze_skill_expert_snapshot(
    snapshot: SkillExpertPreflightSnapshot,
) -> SkillExpertPreflightReport:
    """Return deterministic issues without changing the supplied facts."""

    issues: list[PreflightIssue] = []
    profiles = {item.profile_id: item for item in snapshot.profiles}
    packages = {item.package_id: item for item in snapshot.packages}
    versions = {item.version_id: item for item in snapshot.versions}
    skills = {item.asset_id: item for item in snapshot.skills}
    skill_versions = set(snapshot.skill_versions)

    runnable_names: dict[tuple[int, str], list[int]] = defaultdict(list)
    for profile in snapshot.profiles:
        if profile.status not in {"active", "disabled", "deleted"}:
            issues.append(
                PreflightIssue(
                    severity="migration_required",
                    code="PROFILE_LIFECYCLE_STATUS_REQUIRES_MAPPING",
                    entity_type="runtime_agent_profile",
                    entity_id=str(profile.profile_id),
                )
            )
        if (
            profile.profile_kind == "expert"
            and profile.status not in {"disabled", "deleted"}
        ):
            runnable_names[
                (profile.runtime_workspace_id, _normalized_name(profile.display_name_key))
            ].append(profile.profile_id)
        if profile.applied_revision > profile.desired_revision:
            issues.append(
                PreflightIssue(
                    severity="blocking",
                    code="PROFILE_APPLIED_REVISION_AHEAD_OF_DESIRED",
                    entity_type="runtime_agent_profile",
                    entity_id=str(profile.profile_id),
                )
            )
        elif profile.desired_revision > profile.applied_revision:
            issues.append(
                PreflightIssue(
                    severity="warning",
                    code="PROFILE_CONVERGENCE_INCOMPLETE",
                    entity_type="runtime_agent_profile",
                    entity_id=str(profile.profile_id),
                )
            )
    for (_workspace_id, _name_key), profile_ids in runnable_names.items():
        if len(profile_ids) > 1:
            for profile_id in sorted(profile_ids):
                issues.append(
                    PreflightIssue(
                        severity="blocking",
                        code="WORKSPACE_RUNNABLE_EXPERT_NAME_CONFLICT",
                        entity_type="runtime_agent_profile",
                        entity_id=str(profile_id),
                        related_ids=[
                            str(item) for item in sorted(profile_ids) if item != profile_id
                        ],
                    )
                )

    market_names: dict[tuple[int, str], list[int]] = defaultdict(list)
    for package in snapshot.packages:
        if package.status in {"published", "unlisted", "deleted"}:
            if not package.market_name_key:
                issues.append(
                    PreflightIssue(
                        severity="migration_required",
                        code="MARKET_NAME_LEASE_MISSING",
                        entity_type="expert_package",
                        entity_id=str(package.package_id),
                    )
                )
            else:
                market_names[
                    (package.org_id, _normalized_name(package.market_name_key))
                ].append(package.package_id)
        if package.execution_status not in {"enabled", "force_disabled"}:
            issues.append(
                PreflightIssue(
                    severity="blocking",
                    code="EXPERT_EXECUTION_STATUS_INVALID",
                    entity_type="expert_package",
                    entity_id=str(package.package_id),
                )
            )
        if (
            package.current_version_id is not None
            and package.current_version_id not in versions
        ):
            issues.append(
                PreflightIssue(
                    severity="blocking",
                    code="EXPERT_CURRENT_VERSION_MISSING",
                    entity_type="expert_package",
                    entity_id=str(package.package_id),
                    related_ids=[str(package.current_version_id)],
                )
            )
    for (_org_id, _name_key), package_ids in market_names.items():
        if len(package_ids) > 1:
            for package_id in sorted(package_ids):
                issues.append(
                    PreflightIssue(
                        severity="blocking",
                        code="MARKET_EXPERT_NAME_CONFLICT",
                        entity_type="expert_package",
                        entity_id=str(package_id),
                        related_ids=[
                            str(item) for item in sorted(package_ids) if item != package_id
                        ],
                    )
                )

    active_slots: dict[tuple[int, int], list[int]] = defaultdict(list)
    for binding in snapshot.installations:
        if binding.status == "enabled":
            active_slots[(binding.runtime_workspace_id, binding.package_id)].append(
                binding.binding_id
            )
        profile = profiles.get(binding.profile_id)
        if profile is None:
            issues.append(
                PreflightIssue(
                    severity="blocking",
                    code="INSTALLATION_PROFILE_MISSING",
                    entity_type="installed_expert_binding",
                    entity_id=str(binding.binding_id),
                    related_ids=[str(binding.profile_id)],
                )
            )
        elif profile.runtime_workspace_id != binding.runtime_workspace_id:
            issues.append(
                PreflightIssue(
                    severity="blocking",
                    code="INSTALLATION_PROFILE_WORKSPACE_MISMATCH",
                    entity_type="installed_expert_binding",
                    entity_id=str(binding.binding_id),
                    related_ids=[str(binding.profile_id)],
                )
            )
        if binding.package_id not in packages or binding.version_id not in versions:
            issues.append(
                PreflightIssue(
                    severity="blocking",
                    code="INSTALLATION_EXPERT_REFERENCE_MISSING",
                    entity_type="installed_expert_binding",
                    entity_id=str(binding.binding_id),
                    related_ids=[str(binding.package_id), str(binding.version_id)],
                )
            )
        if binding.status == "enabled" and binding.installation_epoch < 1:
            issues.append(
                PreflightIssue(
                    severity="migration_required",
                    code="INSTALLATION_EPOCH_REQUIRES_BACKFILL",
                    entity_type="installed_expert_binding",
                    entity_id=str(binding.binding_id),
                )
            )
    for binding_ids in active_slots.values():
        if len(binding_ids) > 1:
            for binding_id in sorted(binding_ids):
                issues.append(
                    PreflightIssue(
                        severity="blocking",
                        code="MULTIPLE_ACTIVE_INSTALLATION_SLOTS",
                        entity_type="installed_expert_binding",
                        entity_id=str(binding_id),
                        related_ids=[
                            str(item) for item in sorted(binding_ids) if item != binding_id
                        ],
                    )
                )

    for binding in snapshot.skill_bindings:
        if binding.profile_id not in profiles or binding.skill_id not in skills:
            issues.append(
                PreflightIssue(
                    severity="blocking",
                    code="PROFILE_SKILL_REFERENCE_MISSING",
                    entity_type="profile_skill_binding",
                    entity_id=str(binding.binding_id),
                    related_ids=[str(binding.profile_id), str(binding.skill_id)],
                )
            )
        if (
            binding.skill_version_id is not None
            and binding.skill_version_id not in skill_versions
        ):
            issues.append(
                PreflightIssue(
                    severity="blocking",
                    code="PROFILE_SKILL_VERSION_MISSING",
                    entity_type="profile_skill_binding",
                    entity_id=str(binding.binding_id),
                    related_ids=[str(binding.skill_version_id)],
                )
            )
        if binding.version_policy != "follow_latest":
            issues.append(
                PreflightIssue(
                    severity="migration_required",
                    code="PROFILE_SKILL_POLICY_REQUIRES_UNIFICATION",
                    entity_type="profile_skill_binding",
                    entity_id=str(binding.binding_id),
                )
            )

    for version in snapshot.versions:
        for skill_id in version.skill_ids:
            if skill_id not in skills:
                issues.append(
                    PreflightIssue(
                        severity="blocking",
                        code="EXPERT_SKILL_STABLE_ASSET_MISSING",
                        entity_type="expert_package_version",
                        entity_id=str(version.version_id),
                        related_ids=[str(skill_id)],
                    )
                )
        for reference in version.unresolved_skill_refs:
            issues.append(
                PreflightIssue(
                    severity="blocking",
                    code="EXPERT_SKILL_REFERENCE_UNRESOLVED",
                    entity_type="expert_package_version",
                    entity_id=str(version.version_id),
                    related_ids=[reference],
                )
            )

    for rollout in snapshot.incomplete_rollouts:
        issues.append(
            PreflightIssue(
                severity="warning",
                code="ROLLOUT_INCOMPLETE",
                entity_type="asset_rollout_batch",
                entity_id=rollout.rollout_id,
            )
        )

    issues.sort(
        key=lambda item: (
            {"blocking": 0, "migration_required": 1, "warning": 2}[item.severity],
            item.code,
            item.entity_type,
            item.entity_id,
        )
    )
    counts = {
        "profiles": len(snapshot.profiles),
        "packages": len(snapshot.packages),
        "versions": len(snapshot.versions),
        "installations": len(snapshot.installations),
        "skill_bindings": len(snapshot.skill_bindings),
        "blocking": sum(item.severity == "blocking" for item in issues),
        "migration_required": sum(
            item.severity == "migration_required" for item in issues
        ),
        "warnings": sum(item.severity == "warning" for item in issues),
    }
    return SkillExpertPreflightReport(
        status="blocked" if counts["blocking"] else "ready",
        counts=counts,
        issues=issues,
    )


__all__ = [
    "ExpertPackageFact",
    "ExpertVersionFact",
    "InstallationFact",
    "PreflightIssue",
    "ProfileFact",
    "RolloutFact",
    "SkillBindingFact",
    "SkillExpertPreflightReport",
    "SkillExpertPreflightSnapshot",
    "StableAssetFact",
    "analyze_skill_expert_snapshot",
]
