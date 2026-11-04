from __future__ import annotations

import hmac
from pathlib import Path
import tempfile
import zipfile

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import (
    HermesRuntimeBinding,
    RuntimeAgentProfile,
    RuntimeAgentProfileRevision,
    SkillVersion,
)
from app.services.plaza.public_catalog.package_parser import sha256_directory
from app.services.runtime_projection import (
    RuntimeProjectionAuthenticationError,
    RuntimeProjectionBindingError,
    RuntimeProjectionService,
)


router = APIRouter(tags=["internal-profile-artifacts"])


def _bearer_token(authorization: str | None) -> str:
    scheme, _, token = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runtime artifact authorization required",
        )
    return token


def _remove_file(path: Path) -> None:
    path.unlink(missing_ok=True)


@router.get(
    "/v1/profile-artifacts/{profile_id}/{revision}/{skill_id}/{version_id}",
)
async def download_profile_skill_artifact(
    profile_id: int,
    revision: int,
    skill_id: int,
    version_id: int,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
    runtime_id: str = Header(alias="X-Hermes-Runtime-Id"),
    binding_id: str = Header(alias="X-Hermes-Binding-Id"),
    expected_sha256: str = Header(alias="X-Hermes-Artifact-Sha256"),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    try:
        await RuntimeProjectionService(db).authenticate(
            runtime_id=runtime_id,
            binding_id=binding_id,
            api_server_key=_bearer_token(authorization),
        )
        numeric_binding_id = int(binding_id)
    except RuntimeProjectionAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runtime artifact authorization failed",
        ) from exc
    except (RuntimeProjectionBindingError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Runtime artifact binding is stale",
        ) from exc
    row = (
        await db.execute(
            select(
                HermesRuntimeBinding,
                RuntimeAgentProfile,
                RuntimeAgentProfileRevision,
                SkillVersion,
            )
            .join(
                RuntimeAgentProfile,
                RuntimeAgentProfile.runtime_workspace_id
                == HermesRuntimeBinding.runtime_workspace_id,
            )
            .join(
                RuntimeAgentProfileRevision,
                RuntimeAgentProfileRevision.runtime_agent_profile_id
                == RuntimeAgentProfile.id,
            )
            .join(
                SkillVersion,
                SkillVersion.id == version_id,
            )
            .where(
                HermesRuntimeBinding.id == numeric_binding_id,
                HermesRuntimeBinding.runtime_id == runtime_id,
                RuntimeAgentProfile.id == profile_id,
                RuntimeAgentProfile.org_id
                == RuntimeAgentProfileRevision.org_id,
                RuntimeAgentProfile.user_id
                == RuntimeAgentProfileRevision.user_id,
                RuntimeAgentProfileRevision.revision == revision,
                SkillVersion.skill_id == skill_id,
                SkillVersion.org_id == RuntimeAgentProfile.org_id,
                SkillVersion.status != "deleted",
            )
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profile artifact not found",
        )
    _binding, profile, profile_revision, skill_version = row
    expected = next(
        (
            item
            for item in dict(profile_revision.snapshot or {}).get("skills", [])
            if isinstance(item, dict)
            and int(item.get("skill_id") or 0) == skill_id
            and int(item.get("skill_version_id") or 0) == version_id
        ),
        None,
    )
    if (
        expected is None
        or int(profile.runtime_workspace_id) != int(profile_revision.runtime_workspace_id)
        or not hmac.compare_digest(
            str(expected.get("package_sha256") or "").lower(),
            expected_sha256.lower(),
        )
        or not hmac.compare_digest(
            str(skill_version.package_sha256 or "").lower(),
            expected_sha256.lower(),
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Profile artifact revision is stale",
        )
    source = Path(str(skill_version.package_uri or "")).resolve(strict=False)
    allowed_root = Path(settings.HERMES_PUBLIC_ASSET_ROOT).resolve(strict=False)
    try:
        if (
            not source.is_dir()
            or source.is_symlink()
            or not source.is_relative_to(allowed_root)
            or sha256_directory(source) != expected_sha256.lower()
        ):
            raise ValueError
        files = sorted(item for item in source.rglob("*") if item.is_file())
        if not files or any(item.is_symlink() for item in files):
            raise ValueError
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Profile artifact material is unavailable",
        ) from exc
    temporary = tempfile.NamedTemporaryFile(
        prefix="hermes-profile-artifact-",
        suffix=".zip",
        delete=False,
    )
    temporary_path = Path(temporary.name)
    temporary.close()
    try:
        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for item in files:
                archive.write(item, item.relative_to(source).as_posix())
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    background_tasks.add_task(_remove_file, temporary_path)
    return FileResponse(
        temporary_path,
        media_type="application/zip",
        filename=f"skill-{skill_id}-version-{version_id}.zip",
        headers={
            "Cache-Control": "private, no-store",
            "X-Hermes-Artifact-Sha256": expected_sha256.lower(),
        },
    )


__all__ = ["router"]
