from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import yaml

from app.hermes_runtime.constants import (
    HERMES_PLATFORM_CONFIG_SCHEMA,
    HERMES_PLATFORM_STATE_SCHEMA,
)


CURRENT_HERMES_CONTRACT = "v0.20.1"


class RuntimeImageContractError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeImageContract:
    upstream_version: str
    upstream_commit: str
    state_schema: int
    config_schema: int


def resolve_runtime_image_contract(
    *,
    repository_root: Path,
    image_reference: str,
) -> RuntimeImageContract:
    """Load the one fixed Hermes contract inherited by code-only images."""
    if not image_reference.strip() or "hermes-agent" not in image_reference:
        raise RuntimeImageContractError("Runtime image reference is invalid")
    path = (
        repository_root
        / "runtime"
        / "hermes"
        / "compatibility"
        / f"{CURRENT_HERMES_CONTRACT}.yaml"
    )
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeImageContractError("current Runtime image contract is unreadable") from exc
    if not isinstance(payload, dict):
        raise RuntimeImageContractError("current Runtime image contract is invalid")
    version = str(payload.get("upstream_version") or "")
    commit = str(payload.get("upstream_commit") or "").lower()
    state_schema = payload.get("state_schema")
    config_schema = payload.get("config_schema")
    if (
        version != CURRENT_HERMES_CONTRACT
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
        or state_schema != HERMES_PLATFORM_STATE_SCHEMA
        or config_schema != HERMES_PLATFORM_CONFIG_SCHEMA
    ):
        raise RuntimeImageContractError(
            "current Runtime image contract does not match Backend platform constants"
        )
    return RuntimeImageContract(
        upstream_version=version,
        upstream_commit=commit,
        state_schema=state_schema,
        config_schema=config_schema,
    )


__all__ = [
    "CURRENT_HERMES_CONTRACT",
    "RuntimeImageContract",
    "RuntimeImageContractError",
    "resolve_runtime_image_contract",
]
