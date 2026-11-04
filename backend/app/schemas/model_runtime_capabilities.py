from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


ReasoningReplay = Literal["not_supported", "optional", "required"]
ThinkingMode = Literal["provider_default", "enabled", "disabled"]
VisionMode = Literal["not_supported", "native"]


class RuntimeModelCapabilities(BaseModel):
    """Model-specific runtime wire-protocol requirements.

    The values are signed into every Runtime Session Grant.  Keeping the
    defaults here makes old catalog rows explicitly safe: reasoning fields are
    never sent unless an administrator has published support for them.
    """

    model_config = ConfigDict(extra="forbid")

    reasoning_replay: ReasoningReplay = "not_supported"
    thinking_mode: ThinkingMode = "provider_default"
    # Null keeps the historical wire payload unchanged. Runtime treats a
    # missing value conservatively as text-only.
    vision: VisionMode | None = None


def normalize_runtime_capabilities(value: object | None) -> dict[str, str]:
    return RuntimeModelCapabilities.model_validate(
        value or {}
    ).model_dump(exclude_none=True)


def supports_native_vision(value: object | None) -> bool:
    return normalize_runtime_capabilities(value).get("vision") == "native"
