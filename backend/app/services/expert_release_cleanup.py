from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ExpertPackage,
    ExpertPackageVersion,
    ExpertVersionArtifact,
    InstalledExpertBinding,
    TemplateReview,
)


def _artifact_storage_path(artifact: ExpertVersionArtifact) -> Path | None:
    """Return a release-artifact directory only when it is safely in root."""
    if not artifact.storage_uri:
        return None
    root = Path(settings.HERMES_PUBLIC_ASSET_ROOT).resolve()
    candidate = Path(str(artifact.storage_uri)).resolve()
    try:
        candidate.relative_to(root / "expert-release-artifacts")
    except ValueError:
        return None
    return candidate


async def cleanup_expired_expert_release_artifacts(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    retention_days: int | None = None,
    limit: int = 100,
) -> int:
    """Delete only safely-retired expert release artifacts.

    A package must be withdrawn, have no enabled installation binding and no
    pending review.  Version rows stay for audit/history; this function only
    removes immutable release material after its retention window.
    """
    current = now or datetime.now(timezone.utc)
    retention = (
        settings.EXPERT_RELEASE_ARTIFACT_RETENTION_DAYS
        if retention_days is None
        else retention_days
    )
    cutoff = current - timedelta(days=max(0, int(retention)))
    active_binding = exists(
        select(InstalledExpertBinding.id).where(
            InstalledExpertBinding.expert_package_id == ExpertPackage.id,
            InstalledExpertBinding.status == "enabled",
        )
    )
    pending_review = exists(
        select(TemplateReview.id).where(
            TemplateReview.expert_package_id == ExpertPackage.id,
            TemplateReview.status == "pending",
        )
    )
    artifacts = (
        await db.execute(
            select(ExpertVersionArtifact)
            .join(
                ExpertPackageVersion,
                ExpertPackageVersion.id
                == ExpertVersionArtifact.expert_package_version_id,
            )
            .join(
                ExpertPackage,
                ExpertPackage.id == ExpertPackageVersion.expert_package_id,
            )
            .where(
                ExpertPackage.status == "withdrawn",
                ~active_binding,
                ~pending_review,
                ExpertVersionArtifact.created_at <= cutoff,
            )
            .order_by(ExpertVersionArtifact.id.asc())
            .limit(max(1, limit))
        )
    ).scalars().all()

    # Move directories out of the live release tree before deleting DB rows.
    # If that move fails, preserve the row and retry later; never lose the only
    # copy before the database operation is durable.
    moved: list[tuple[Path, Path]] = []
    trash_root = (
        Path(settings.HERMES_PUBLIC_ASSET_ROOT)
        / "expert-release-artifacts"
        / ".gc"
    )
    eligible: list[ExpertVersionArtifact] = []
    try:
        for artifact in artifacts:
            source = _artifact_storage_path(artifact)
            if source is not None and source.exists():
                trash_root.mkdir(parents=True, exist_ok=True)
                destination = trash_root / f"artifact-{int(artifact.id)}"
                shutil.rmtree(destination, ignore_errors=True)
                source.replace(destination)
                moved.append((source, destination))
            eligible.append(artifact)
        for artifact in eligible:
            await db.delete(artifact)
        await db.commit()
    except Exception:
        await db.rollback()
        for source, destination in reversed(moved):
            if destination.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                destination.replace(source)
        raise

    for _source, destination in moved:
        shutil.rmtree(destination, ignore_errors=True)
    return len(eligible)
