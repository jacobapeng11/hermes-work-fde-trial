from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PlatformStreamEvent:
    """Backend channel event contract emitted by active Runtime adapters."""

    event: str
    data: dict[str, Any]
    # Private backend-only metadata. It must never be serialized, persisted,
    # or handed to the browser event broker.
    internal: object | None = None

    def to_sse(self) -> str:
        import json

        return f"event: {self.event}\ndata: {json.dumps(self.data, ensure_ascii=False)}\n\n"
