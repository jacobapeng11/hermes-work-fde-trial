#!/usr/bin/env python3
"""Read-only preflight for the one-time platform asset model cutover."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

from sqlalchemy import select


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
INVOCATION_DIRECTORY = Path.cwd()
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

from app.database import AsyncSessionLocal, engine  # noqa: E402
from app.models import (  # noqa: E402
    ExpertPackage,
    ExpertPackageVersion,
    ExpertVersionArtifact,
    Mcp,
    RuntimeAgentProfile,
    Skill,
    SkillVersion,
)
from app.models.legacy_assets import (  # noqa: E402
    LegacyProfileAssetDraft as ProfileAssetDraft,
    LegacyPublicMcpAsset as PublicMcpAsset,
    LegacyPublicSkillAsset as PublicSkillAsset,
    LegacyPublicSkillVersion as PublicSkillVersion,
    LegacyRuntimeKbBinding as RuntimeKbBinding,
    LegacyRuntimeMcpBinding as RuntimeMcpBinding,
    LegacyRuntimeSkillBinding as RuntimeSkillBinding,
    LegacyUserRuntimeMcp as UserRuntimeMcp,
    LegacyUserRuntimeSkill as UserRuntimeSkill,
)
from app.services.asset_unification_preflight import (  # noqa: E402
    AssetUnificationPreflightSnapshot,
    LegacyAssetBindingFact,
    LegacyAssetVersionFact,
    LegacyDraftFact,
    LegacyProfileFact,
    analyze_asset_unification_snapshot,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Read current asset/Profile facts and report one-time cutover conflicts. "
            "This command never writes MySQL, Runtime, Dashboard, or PVC state."
        )
    )
    result.add_argument(
        "--snapshot",
        type=_snapshot_path,
        help="Analyze a previously exported JSON snapshot instead of MySQL.",
    )
    return result


def _snapshot_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = INVOCATION_DIRECTORY / path
    return path.resolve()


def _digest(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _runtime_asset_key(asset_type: str, value: object) -> str:
    return f"{asset_type}-runtime-name:{str(value).strip().casefold()}"


def _profile_identity(
    profiles: dict[int, RuntimeAgentProfile], profile_id: int
) -> tuple[int, int, int]:
    profile = profiles.get(profile_id)
    if profile is None:
        return 1, 1, 1
    return int(profile.org_id), int(profile.user_id), int(profile.runtime_workspace_id)


async def collect_snapshot() -> AssetUnificationPreflightSnapshot:
    async with AsyncSessionLocal() as db:
        profile_models = list((await db.scalars(select(RuntimeAgentProfile))).all())
        profiles = {int(item.id): item for item in profile_models}
        profile_facts = [
            LegacyProfileFact(
                profile_id=int(item.id),
                org_id=int(item.org_id),
                user_id=int(item.user_id),
                runtime_workspace_id=int(item.runtime_workspace_id),
                profile_kind=(
                    "default" if str(item.profile_kind) == "default" else "expert"
                ),
            )
            for item in profile_models
        ]
        bindings: list[LegacyAssetBindingFact] = []

        for item in (await db.scalars(select(UserRuntimeSkill))).all():
            if item.public_skill_asset_id is not None:
                logical_key = _runtime_asset_key("skill", item.runtime_skill_name)
            else:
                logical_key = _runtime_asset_key("skill", item.runtime_skill_name)
            bindings.append(
                LegacyAssetBindingFact(
                    source_table="user_runtime_skill",
                    source_id=int(item.id),
                    org_id=int(item.org_id),
                    user_id=int(item.user_id),
                    runtime_workspace_id=int(item.runtime_workspace_id),
                    profile_id=int(item.runtime_agent_profile_id),
                    asset_type="skill",
                    logical_asset_key=logical_key,
                    version_key=str(
                        item.installed_public_skill_version_id or item.version_label
                    ),
                    content_digest=f"sha256:{str(item.installed_package_sha256).lower()}",
                )
            )

        skill_rows = (
            await db.execute(
                select(RuntimeSkillBinding, Skill, SkillVersion)
                .join(Skill, Skill.id == RuntimeSkillBinding.skill_id)
                .outerjoin(
                    SkillVersion,
                    SkillVersion.id == RuntimeSkillBinding.skill_version_id,
                )
            )
        ).all()
        for item, skill, version in skill_rows:
            org_id, user_id, workspace_id = _profile_identity(
                profiles, int(item.runtime_agent_profile_id)
            )
            bindings.append(
                LegacyAssetBindingFact(
                    source_table="runtime_skill_binding",
                    source_id=int(item.id),
                    org_id=org_id,
                    user_id=user_id,
                    runtime_workspace_id=workspace_id,
                    profile_id=int(item.runtime_agent_profile_id),
                    asset_type="skill",
                    logical_asset_key=_runtime_asset_key("skill", skill.slug),
                    version_key=(str(version.version) if version is not None else None),
                    content_digest=(
                        f"sha256:{str(version.package_sha256).lower()}"
                        if version is not None and version.package_sha256
                        else _digest(version.manifest or {})
                        if version is not None
                        else None
                    ),
                    artifact_uri=(
                        str(version.package_uri)
                        if version is not None and version.package_uri
                        else None
                    ),
                )
            )

        public_mcp_rows = (
            await db.execute(
                select(UserRuntimeMcp, PublicMcpAsset).join(
                    PublicMcpAsset,
                    PublicMcpAsset.id == UserRuntimeMcp.public_mcp_asset_id,
                )
            )
        ).all()
        for item, mcp in public_mcp_rows:
            bindings.append(
                LegacyAssetBindingFact(
                    source_table="user_runtime_mcp",
                    source_id=int(item.id),
                    org_id=int(item.org_id),
                    user_id=int(item.user_id),
                    runtime_workspace_id=int(item.runtime_workspace_id),
                    profile_id=int(item.runtime_agent_profile_id),
                    asset_type="mcp",
                    logical_asset_key=_runtime_asset_key("mcp", mcp.slug),
                    version_key="legacy-head",
                    content_digest=_digest(mcp.runtime_config),
                )
            )

        mcp_rows = (
            await db.execute(
                select(RuntimeMcpBinding, Mcp).join(
                    Mcp, Mcp.id == RuntimeMcpBinding.mcp_id
                )
            )
        ).all()
        for item, mcp in mcp_rows:
            org_id, user_id, workspace_id = _profile_identity(
                profiles, int(item.runtime_agent_profile_id)
            )
            bindings.append(
                LegacyAssetBindingFact(
                    source_table="runtime_mcp_binding",
                    source_id=int(item.id),
                    org_id=org_id,
                    user_id=user_id,
                    runtime_workspace_id=workspace_id,
                    profile_id=int(item.runtime_agent_profile_id),
                    asset_type="mcp",
                    logical_asset_key=_runtime_asset_key("mcp", mcp.slug),
                    version_key="legacy-head",
                    content_digest=_digest(mcp.config),
                )
            )

        for item in (await db.scalars(select(RuntimeKbBinding))).all():
            org_id, user_id, workspace_id = _profile_identity(
                profiles, int(item.runtime_agent_profile_id)
            )
            bindings.append(
                LegacyAssetBindingFact(
                    source_table="runtime_kb_binding",
                    source_id=int(item.id),
                    org_id=org_id,
                    user_id=user_id,
                    runtime_workspace_id=workspace_id,
                    profile_id=int(item.runtime_agent_profile_id),
                    asset_type="knowledge",
                    logical_asset_key=f"knowledge:{int(item.knowledge_base_id)}",
                )
            )

        skill_ids = set(await db.scalars(select(Skill.id)))
        public_skill_ids = set(await db.scalars(select(PublicSkillAsset.id)))
        expert_ids = set(await db.scalars(select(ExpertPackage.id)))
        versions = [
            LegacyAssetVersionFact(
                source_table="skill_version",
                source_id=int(item.id),
                parent_table="skill",
                parent_id=int(item.skill_id),
                parent_exists=int(item.skill_id) in skill_ids,
                artifact_required=True,
                artifact_uri=str(item.package_uri) if item.package_uri else None,
            )
            for item in (await db.scalars(select(SkillVersion))).all()
        ]
        versions.extend(
            LegacyAssetVersionFact(
                source_table="public_skill_version",
                source_id=int(item.id),
                parent_table="public_skill_asset",
                parent_id=int(item.public_skill_asset_id),
                parent_exists=int(item.public_skill_asset_id) in public_skill_ids,
                artifact_required=True,
                artifact_uri=str(item.deployment_package_uri),
            )
            for item in (await db.scalars(select(PublicSkillVersion))).all()
        )
        versions.extend(
            LegacyAssetVersionFact(
                source_table="expert_package_version",
                source_id=int(item.id),
                parent_table="expert_package",
                parent_id=int(item.expert_package_id),
                parent_exists=int(item.expert_package_id) in expert_ids,
            )
            for item in (await db.scalars(select(ExpertPackageVersion))).all()
        )
        expert_version_ids = set(
            await db.scalars(select(ExpertPackageVersion.id))
        )
        versions.extend(
            LegacyAssetVersionFact(
                source_table="expert_version_artifact",
                source_id=int(item.id),
                parent_table="expert_package_version",
                parent_id=int(item.expert_package_version_id),
                parent_exists=(
                    int(item.expert_package_version_id) in expert_version_ids
                ),
                artifact_required=str(item.artifact_type) == "skill_package",
                artifact_uri=str(item.storage_uri) if item.storage_uri else None,
            )
            for item in (await db.scalars(select(ExpertVersionArtifact))).all()
        )
        drafts = [
            LegacyDraftFact(
                source_id=int(item.id),
                runtime_workspace_id=int(item.profile_id),
                status=str(item.status),
            )
            for item in (await db.scalars(select(ProfileAssetDraft))).all()
        ]
    return AssetUnificationPreflightSnapshot(
        profiles=profile_facts,
        bindings=bindings,
        versions=versions,
        drafts=drafts,
    )


async def async_main(args: argparse.Namespace) -> int:
    try:
        if args.snapshot is not None:
            snapshot = AssetUnificationPreflightSnapshot.model_validate_json(
                args.snapshot.read_text(encoding="utf-8")
            )
        else:
            snapshot = await collect_snapshot()
        report = analyze_asset_unification_snapshot(snapshot)
        print(report.model_dump_json(indent=2))
        return 0 if report.ready else 2
    finally:
        await engine.dispose()


def main() -> int:
    return asyncio.run(async_main(parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
