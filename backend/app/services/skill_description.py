from __future__ import annotations

from collections.abc import Mapping
from typing import Any


MAX_SKILL_DESCRIPTION_LENGTH = 2000


class SkillDescriptionError(ValueError):
    """Raised when a user-provided Skill description is invalid."""


def normalize_skill_description(
    value: str | None,
    *,
    default: str | None = None,
) -> str | None:
    """Return custom text when present, otherwise the package default."""
    if value is not None:
        if not isinstance(value, str):
            raise SkillDescriptionError("skill_description_invalid")
        normalized = value.strip()
        if normalized:
            if len(normalized) > MAX_SKILL_DESCRIPTION_LENGTH:
                raise SkillDescriptionError("skill_description_too_long")
            return normalized
    return _clean_default(default)


def default_skill_description(manifest: Mapping[str, Any] | None) -> str | None:
    if not isinstance(manifest, Mapping):
        return None
    metadata = manifest.get("skill_metadata")
    if not isinstance(metadata, Mapping):
        return None
    value = metadata.get("description")
    return _clean_default(value if isinstance(value, str) else None)


def _clean_default(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


__all__ = [
    "MAX_SKILL_DESCRIPTION_LENGTH",
    "SkillDescriptionError",
    "default_skill_description",
    "normalize_skill_description",
]
