from __future__ import annotations

from pydantic import BaseModel


class AvatarAssetSummary(BaseModel):
    avatar_value: str
    label: str
    source_kind: str
    image_url: str | None = None
    sort_order: int
