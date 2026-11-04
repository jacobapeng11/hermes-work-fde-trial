from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field


Severity = Literal["info", "warning", "blocking"]


class StrictPreflightModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LegacyProfileFact(StrictPreflightModel):
    profile_id: int = Field(gt=0)
    org_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    runtime_workspace_id: int = Field(gt=0)
    profile_kind: Literal["default", "expert"]


class LegacyAssetBindingFact(StrictPreflightModel):
    source_table: str = Field(min_length=1, max_length=128)
    source_id: int = Field(gt=0)
    org_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    runtime_workspace_id: int = Field(gt=0)
    profile_id: int = Field(gt=0)
    asset_type: Literal["skill", "mcp", "knowledge"]
    logical_asset_key: str = Field(min_length=1, max_length=256)
    version_key: str | None = Field(default=None, max_length=256)
    content_digest: str | None = Field(default=None, max_length=128)
    artifact_uri: str | None = Field(default=None, max_length=1000)


class LegacyAssetVersionFact(StrictPreflightModel):
    source_table: str = Field(min_length=1, max_length=128)
    source_id: int = Field(gt=0)
    parent_table: str = Field(min_length=1, max_length=128)
    parent_id: int = Field(gt=0)
    parent_exists: bool
    artifact_required: bool = False
    artifact_uri: str | None = Field(default=None, max_length=1000)


class LegacyDraftFact(StrictPreflightModel):
    source_id: int = Field(gt=0)
    runtime_workspace_id: int = Field(gt=0)
    status: str = Field(min_length=1, max_length=32)


class AssetUnificationPreflightSnapshot(StrictPreflightModel):
    profiles: list[LegacyProfileFact] = Field(default_factory=list)
    bindings: list[LegacyAssetBindingFact] = Field(default_factory=list)
    versions: list[LegacyAssetVersionFact] = Field(default_factory=list)
    drafts: list[LegacyDraftFact] = Field(default_factory=list)


class AssetUnificationPreflightIssue(StrictPreflightModel):
    code: str
    severity: Severity
    message: str
    user_id: int | None = None
    runtime_workspace_id: int | None = None
    profile_id: int | None = None
    source_refs: list[str] = Field(default_factory=list)


class AssetUnificationPreflightReport(StrictPreflightModel):
    ready: bool
    profile_count: int
    binding_count: int
    version_count: int
    affected_user_count: int
    affected_profile_count: int
    blocking_issue_count: int
    warning_issue_count: int
    mergeable_duplicate_count: int
    issues: list[AssetUnificationPreflightIssue]


def _local_artifact_path(uri: str | None) -> Path | None:
    if not uri:
        return None
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return Path(unquote(parsed.path))
    if parsed.scheme:
        return None
    path = Path(uri)
    return path if path.is_absolute() else None


def _binding_identity(binding: LegacyAssetBindingFact) -> str:
    if binding.content_digest:
        normalized = binding.content_digest.strip().lower()
        return normalized if normalized.startswith("sha256:") else f"sha256:{normalized}"
    return f"version:{binding.version_key or '<missing>'}"


def analyze_asset_unification_snapshot(
    snapshot: AssetUnificationPreflightSnapshot,
) -> AssetUnificationPreflightReport:
    issues: list[AssetUnificationPreflightIssue] = []
    profiles = {profile.profile_id: profile for profile in snapshot.profiles}

    by_workspace: dict[int, list[LegacyProfileFact]] = defaultdict(list)
    for profile in snapshot.profiles:
        by_workspace[profile.runtime_workspace_id].append(profile)
    for workspace_id, workspace_profiles in sorted(by_workspace.items()):
        defaults = [item for item in workspace_profiles if item.profile_kind == "default"]
        if len(defaults) != 1:
            issues.append(
                AssetUnificationPreflightIssue(
                    code="DEFAULT_PROFILE_COUNT_INVALID",
                    severity="blocking",
                    message=(
                        f"Runtime Workspace {workspace_id} has {len(defaults)} default Profiles; "
                        "exactly one is required"
                    ),
                    user_id=workspace_profiles[0].user_id,
                    runtime_workspace_id=workspace_id,
                    source_refs=[f"runtime_agent_profile:{item.profile_id}" for item in defaults],
                )
            )

    grouped: dict[
        tuple[int, str, str], list[LegacyAssetBindingFact]
    ] = defaultdict(list)
    for binding in snapshot.bindings:
        profile = profiles.get(binding.profile_id)
        if profile is None:
            issues.append(
                AssetUnificationPreflightIssue(
                    code="ORPHAN_PROFILE_BINDING",
                    severity="blocking",
                    message=f"{binding.source_table}:{binding.source_id} references a missing Profile",
                    user_id=binding.user_id,
                    runtime_workspace_id=binding.runtime_workspace_id,
                    profile_id=binding.profile_id,
                    source_refs=[f"{binding.source_table}:{binding.source_id}"],
                )
            )
        elif (
            profile.org_id != binding.org_id
            or profile.user_id != binding.user_id
            or profile.runtime_workspace_id != binding.runtime_workspace_id
        ):
            issues.append(
                AssetUnificationPreflightIssue(
                    code="BINDING_PROFILE_IDENTITY_CONFLICT",
                    severity="blocking",
                    message=f"{binding.source_table}:{binding.source_id} identity differs from its Profile",
                    user_id=binding.user_id,
                    runtime_workspace_id=binding.runtime_workspace_id,
                    profile_id=binding.profile_id,
                    source_refs=[f"{binding.source_table}:{binding.source_id}"],
                )
            )
        grouped[
            (binding.profile_id, binding.asset_type, binding.logical_asset_key)
        ].append(binding)

        local_path = _local_artifact_path(binding.artifact_uri)
        if local_path is not None and not local_path.exists():
            issues.append(
                AssetUnificationPreflightIssue(
                    code="ASSET_ARTIFACT_MISSING",
                    severity="blocking",
                    message=f"Asset artifact is missing for {binding.source_table}:{binding.source_id}",
                    user_id=binding.user_id,
                    runtime_workspace_id=binding.runtime_workspace_id,
                    profile_id=binding.profile_id,
                    source_refs=[f"{binding.source_table}:{binding.source_id}"],
                )
            )

    for (profile_id, _asset_type, logical_key), bindings in sorted(grouped.items()):
        if len(bindings) < 2:
            continue
        identities = {_binding_identity(item) for item in bindings}
        severity: Severity = "info" if len(identities) == 1 else "blocking"
        code = (
            "DUPLICATE_BINDING_MERGEABLE"
            if severity == "info"
            else "DUPLICATE_BINDING_CONFLICT"
        )
        profile = profiles.get(profile_id)
        issues.append(
            AssetUnificationPreflightIssue(
                code=code,
                severity=severity,
                message=(
                    f"Profile {profile_id} has {len(bindings)} facts for {logical_key}; "
                    + ("content is identical" if severity == "info" else "versions or digests conflict")
                ),
                user_id=profile.user_id if profile else bindings[0].user_id,
                runtime_workspace_id=(
                    profile.runtime_workspace_id
                    if profile
                    else bindings[0].runtime_workspace_id
                ),
                profile_id=profile_id,
                source_refs=[
                    f"{item.source_table}:{item.source_id}"
                    for item in sorted(
                        bindings, key=lambda item: (item.source_table, item.source_id)
                    )
                ],
            )
        )

    for version in sorted(
        snapshot.versions, key=lambda item: (item.source_table, item.source_id)
    ):
        if not version.parent_exists:
            issues.append(
                AssetUnificationPreflightIssue(
                    code="ORPHAN_ASSET_VERSION",
                    severity="blocking",
                    message=f"{version.source_table}:{version.source_id} has no parent asset",
                    source_refs=[
                        f"{version.source_table}:{version.source_id}",
                        f"{version.parent_table}:{version.parent_id}",
                    ],
                )
            )
        if version.artifact_required and not version.artifact_uri:
            issues.append(
                AssetUnificationPreflightIssue(
                    code="VERSION_ARTIFACT_URI_MISSING",
                    severity="blocking",
                    message=f"Artifact location is missing for {version.source_table}:{version.source_id}",
                    source_refs=[f"{version.source_table}:{version.source_id}"],
                )
            )
            continue
        local_path = _local_artifact_path(version.artifact_uri)
        if local_path is not None and not local_path.exists():
            issues.append(
                AssetUnificationPreflightIssue(
                    code="VERSION_ARTIFACT_MISSING",
                    severity="blocking",
                    message=f"Version artifact is missing for {version.source_table}:{version.source_id}",
                    source_refs=[f"{version.source_table}:{version.source_id}"],
                )
            )

    for draft in sorted(snapshot.drafts, key=lambda item: item.source_id):
        if draft.status not in {"discarded", "applied"}:
            issues.append(
                AssetUnificationPreflightIssue(
                    code="UNSAVED_PROFILE_DRAFT",
                    severity="blocking",
                    message=f"Profile asset draft {draft.source_id} is still {draft.status}",
                    runtime_workspace_id=draft.runtime_workspace_id,
                    source_refs=[f"profile_asset_draft:{draft.source_id}"],
                )
            )

    issues.sort(
        key=lambda item: (
            0 if item.severity == "blocking" else 1 if item.severity == "warning" else 2,
            item.code,
            item.runtime_workspace_id or 0,
            item.profile_id or 0,
            item.source_refs,
        )
    )
    blocking = sum(issue.severity == "blocking" for issue in issues)
    warning = sum(issue.severity == "warning" for issue in issues)
    affected_users = {item.user_id for item in snapshot.bindings}
    affected_profiles = {item.profile_id for item in snapshot.bindings}
    return AssetUnificationPreflightReport(
        ready=blocking == 0,
        profile_count=len(snapshot.profiles),
        binding_count=len(snapshot.bindings),
        version_count=len(snapshot.versions),
        affected_user_count=len(affected_users),
        affected_profile_count=len(affected_profiles),
        blocking_issue_count=blocking,
        warning_issue_count=warning,
        mergeable_duplicate_count=sum(
            issue.code == "DUPLICATE_BINDING_MERGEABLE" for issue in issues
        ),
        issues=issues,
    )
