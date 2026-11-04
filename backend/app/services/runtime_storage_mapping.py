from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import CloudHermesProfile, HermesRuntimeBinding

class RuntimeStorageMappingError(ValueError):
    """Raised when an operation cannot be mapped to its owned Runtime home."""


@dataclass(frozen=True)
class RuntimeStorageLocation:
    root: Path
    home: Path
    workspace: Path


class RuntimeStorageMappingResolver:
    """Resolve canonical host storage from persisted Runtime identity only."""

    def __init__(self, db: AsyncSession, *, runtime_root: str | Path | None = None) -> None:
        self.db = db
        self.runtime_root = Path(runtime_root or settings.HERMES_RUNTIME_ROOT)

    async def resolve(
        self, *, org_id: int, user_id: int, runtime_workspace_id: int
    ) -> RuntimeStorageLocation:
        """Resolve a Runtime home from an already-authorized owner scope.

        Storage mapping is shared by direct Skill/MCP executors and must not
        depend on the retired durable operation record merely to carry three
        identity fields.
        """
        if org_id <= 0 or user_id <= 0 or runtime_workspace_id <= 0:
            raise RuntimeStorageMappingError("runtime storage identity is invalid")
        result = await self.db.execute(
            select(HermesRuntimeBinding, CloudHermesProfile)
            .join(CloudHermesProfile, CloudHermesProfile.id == HermesRuntimeBinding.runtime_workspace_id)
            .where(HermesRuntimeBinding.runtime_workspace_id == runtime_workspace_id)
        )
        row = result.one_or_none()
        if row is None:
            raise RuntimeStorageMappingError("runtime storage binding not found")
        binding, profile = row
        self._validate_ownership(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            binding=binding,
            profile=profile,
        )

        root = self._resolve_root(binding)
        home = self._resolve_subpath(root, getattr(binding, "hermes_home_sub_path", None), "home")
        workspace = self._resolve_subpath(root, getattr(binding, "workspace_sub_path", None), "workspace")
        return RuntimeStorageLocation(root=root, home=home, workspace=workspace)

    def _resolve_root(self, binding: Any) -> Path:
        boundary = self.runtime_root.resolve(strict=False)
        persisted = (getattr(binding, "runtime_root_host_path", None) or "").strip()
        if not persisted:
            raise RuntimeStorageMappingError(
                "persisted runtime storage root is missing"
            )
        candidate = Path(persisted).resolve(strict=False)
        if not candidate.is_relative_to(boundary):
            raise RuntimeStorageMappingError("runtime storage root is outside the canonical root")
        return candidate

    @staticmethod
    def _resolve_subpath(root: Path, raw_subpath: Any, label: str) -> Path:
        if not isinstance(raw_subpath, str) or not raw_subpath:
            raise RuntimeStorageMappingError(f"runtime {label} subpath is invalid")
        relative = PurePosixPath(raw_subpath.replace("\\", "/"))
        if not relative.parts or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise RuntimeStorageMappingError(f"runtime {label} subpath is unsafe")
        boundary = root.resolve(strict=False)
        candidate = (root / Path(*relative.parts)).resolve(strict=False)
        if not candidate.is_relative_to(boundary) or not candidate.parent.is_relative_to(boundary):
            raise RuntimeStorageMappingError(f"runtime {label} subpath escapes the runtime root")
        return candidate

    @staticmethod
    def _validate_ownership(
        *, org_id: int, user_id: int, runtime_workspace_id: int, binding: Any, profile: Any
    ) -> None:
        expected = (org_id, user_id, runtime_workspace_id)
        binding_identity = (
            getattr(binding, "org_id", None),
            getattr(binding, "user_id", None),
            getattr(binding, "runtime_workspace_id", None),
        )
        profile_identity = (
            getattr(profile, "org_id", None),
            getattr(profile, "user_id", None),
            getattr(profile, "id", None),
        )
        if binding_identity != expected or profile_identity != expected:
            raise RuntimeStorageMappingError("runtime storage ownership mismatch")
        if getattr(profile, "status", None) != "active":
            raise RuntimeStorageMappingError("runtime profile is not active")
        if getattr(binding, "status", None) != "ready":
            raise RuntimeStorageMappingError("runtime binding is not ready")
        if getattr(binding, "runtime_config_source", None) != "pvc":
            raise RuntimeStorageMappingError("runtime storage source is not PVC")
