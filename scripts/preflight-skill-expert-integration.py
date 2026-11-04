#!/usr/bin/env python3
"""Read-only preflight for the Skill/Expert semantic cutover."""

from __future__ import annotations

import argparse
import asyncio
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
    AssetRolloutBatch,
    ExpertPackage,
    ExpertPackageVersion,
    InstalledExpertBinding,
    ProfileSkillBinding,
    RuntimeAgentProfile,
    Skill,
    SkillVersion,
)
from app.services.skill_expert_integration_preflight import (  # noqa: E402
    ExpertPackageFact,
    ExpertVersionFact,
    InstallationFact,
    ProfileFact,
    RolloutFact,
    SkillBindingFact,
    SkillExpertPreflightSnapshot,
    StableAssetFact,
    analyze_skill_expert_snapshot,
)


def _snapshot_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = INVOCATION_DIRECTORY / path
    return path.resolve()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Read current Skill/Expert/Profile facts and report cutover blockers. "
            "This command never writes MySQL, Runtime, Kubernetes, or PVC state."
        )
    )
    result.add_argument(
        "--snapshot",
        type=_snapshot_path,
        help="Analyze a previously exported JSON snapshot instead of MySQL.",
    )
    return result


def _skill_refs(version: ExpertPackageVersion) -> tuple[list[int], list[str]]:
    stable_ids: list[int] = []
    unresolved: list[str] = []
    for index, reference in enumerate(list(version.skill_refs or [])):
        if not isinstance(reference, dict):
            unresolved.append(f"ref:{index}")
            continue
        raw_id = reference.get("skill_id") or reference.get("asset_id")
        try:
            skill_id = int(raw_id)
        except (TypeError, ValueError):
            unresolved.append(f"ref:{index}")
            continue
        if skill_id <= 0:
            unresolved.append(f"ref:{index}")
            continue
        stable_ids.append(skill_id)
    return sorted(set(stable_ids)), unresolved


async def collect_snapshot() -> SkillExpertPreflightSnapshot:
    async with AsyncSessionLocal() as db:
        profile_rows = list(await db.scalars(select(RuntimeAgentProfile)))
        package_rows = list(await db.scalars(select(ExpertPackage)))
        version_rows = list(await db.scalars(select(ExpertPackageVersion)))
        installation_rows = list(await db.scalars(select(InstalledExpertBinding)))
        skill_binding_rows = list(await db.scalars(select(ProfileSkillBinding)))
        skill_rows = list(await db.scalars(select(Skill)))
        skill_version_ids = [int(item) for item in await db.scalars(select(SkillVersion.id))]
        rollout_rows = list(
            await db.scalars(
                select(AssetRolloutBatch).where(
                    AssetRolloutBatch.status.notin_(("completed", "failed"))
                )
            )
        )

    versions: list[ExpertVersionFact] = []
    for item in version_rows:
        skill_ids, unresolved = _skill_refs(item)
        versions.append(
            ExpertVersionFact(
                version_id=int(item.id),
                package_id=int(item.expert_package_id),
                status=str(item.status),
                skill_ids=skill_ids,
                unresolved_skill_refs=unresolved,
            )
        )
    return SkillExpertPreflightSnapshot(
        profiles=[
            ProfileFact(
                profile_id=int(item.id),
                runtime_workspace_id=int(item.runtime_workspace_id),
                profile_kind=str(item.profile_kind),
                ownership_type=str(item.ownership_type),
                status=str(item.status),
                display_name_key=str(item.display_name),
                desired_revision=int(item.desired_revision or 0),
                applied_revision=int(item.applied_revision or 0),
            )
            for item in profile_rows
        ],
        packages=[
            ExpertPackageFact(
                package_id=int(item.id),
                org_id=int(item.org_id),
                status=str(item.status),
                execution_status=str(item.execution_status),
                market_name_key=(
                    str(item.market_name_key)
                    if item.market_name_key is not None
                    else None
                ),
                current_version_id=(
                    int(item.current_version_id)
                    if item.current_version_id is not None
                    else None
                ),
            )
            for item in package_rows
        ],
        versions=versions,
        installations=[
            InstallationFact(
                binding_id=int(item.id),
                runtime_workspace_id=int(item.runtime_workspace_id),
                package_id=int(item.expert_package_id),
                version_id=int(item.expert_package_version_id),
                profile_id=int(item.runtime_agent_profile_id),
                status=str(item.status),
                installation_epoch=int(item.installation_epoch or 0),
            )
            for item in installation_rows
        ],
        skill_bindings=[
            SkillBindingFact(
                binding_id=int(item.id),
                profile_id=int(item.runtime_agent_profile_id),
                skill_id=int(item.skill_id),
                skill_version_id=(
                    int(item.skill_version_id)
                    if item.skill_version_id is not None
                    else None
                ),
                version_policy=str(item.version_policy),
                status=str(item.status),
            )
            for item in skill_binding_rows
        ],
        skills=[
            StableAssetFact(
                asset_id=int(item.id),
                current_version_id=(
                    int(item.current_version_id)
                    if item.current_version_id is not None
                    else None
                ),
            )
            for item in skill_rows
        ],
        skill_versions=skill_version_ids,
        incomplete_rollouts=[
            RolloutFact(rollout_id=str(item.rollout_id), status=str(item.status))
            for item in rollout_rows
        ],
    )


async def run(args: argparse.Namespace) -> int:
    if args.snapshot:
        snapshot = SkillExpertPreflightSnapshot.model_validate_json(
            args.snapshot.read_text(encoding="utf-8")
        )
    else:
        snapshot = await collect_snapshot()
    report = analyze_skill_expert_snapshot(snapshot)
    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    if not args.snapshot:
        await engine.dispose()
    return 2 if report.status == "blocked" else 0


def main() -> int:
    return asyncio.run(run(parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
