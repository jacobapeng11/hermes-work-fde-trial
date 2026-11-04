from __future__ import annotations

import hashlib
import json
from typing import Any


def application_asset_digests(config: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return stable profile/component digests for one app config snapshot."""
    snapshot = config.get("asset_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}

    def digest(value: object) -> str:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    return (
        digest(config),
        digest([snapshot.get("skills", []), snapshot.get("application_skills", [])]),
        digest(snapshot.get("mcps", [])),
        digest(snapshot.get("knowledge_base_ids", [])),
    )


__all__ = ["application_asset_digests"]
