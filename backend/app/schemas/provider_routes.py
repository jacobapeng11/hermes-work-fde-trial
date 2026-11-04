from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProviderRouteGrantV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = 1
    capsule: str = Field(min_length=16, max_length=8192)
    digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


__all__ = ["ProviderRouteGrantV1"]
