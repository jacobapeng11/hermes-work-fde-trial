from __future__ import annotations

import re

from app.services.runtime_image_config import RuntimeImageConfig


_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")


class RuntimeImageVerificationError(RuntimeError):
    pass


def live_image_id_digest(image_id: str) -> str:
    matches = _DIGEST.findall(str(image_id or ""))
    if len(matches) != 1:
        raise RuntimeImageVerificationError("live Pod imageID is not one sha256 identity")
    return matches[0]


def require_live_image_matches_target(
    image_id: str,
    target: RuntimeImageConfig,
) -> str:
    if not target.manifest_digest or not target.config_id:
        raise RuntimeImageVerificationError("target image identity is incomplete")
    observed = live_image_id_digest(image_id)
    if observed not in {target.manifest_digest, target.config_id}:
        raise RuntimeImageVerificationError("live Pod imageID does not match target identity")
    return observed


__all__ = [
    "RuntimeImageVerificationError",
    "live_image_id_digest",
    "require_live_image_matches_target",
]
