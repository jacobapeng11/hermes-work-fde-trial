from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import quote


_SIGNATURE_VERSION = "openapi-file-download-v1"


def _signature(
    *,
    slug: str,
    file_id: int,
    expires: int,
    secret: str,
) -> str:
    message = f"{_SIGNATURE_VERSION}\n{slug}\n{int(file_id)}\n{int(expires)}"
    return hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def sign_file_download_url(
    *,
    base_url: str,
    slug: str,
    file_id: int,
    secret: str,
    ttl_seconds: int,
    issued_at: int | None = None,
) -> str:
    if not secret:
        raise ValueError("file download signing secret is not configured")
    if ttl_seconds <= 0:
        raise ValueError("file download TTL must be positive")
    issued = int(time.time()) if issued_at is None else int(issued_at)
    expires = issued + int(ttl_seconds)
    signature = _signature(
        slug=slug,
        file_id=file_id,
        expires=expires,
        secret=secret,
    )
    base = base_url.rstrip("/")
    encoded_slug = quote(slug, safe="")
    return (
        f"{base}/openapi/v1/application-agents/{encoded_slug}"
        f"/files/{int(file_id)}/content"
        f"?expires={expires}&signature={signature}"
    )


def verify_file_download_signature(
    *,
    slug: str,
    file_id: int,
    expires: int,
    signature: str,
    secret: str,
    now: int | None = None,
) -> bool:
    if not secret or not signature:
        return False
    current = int(time.time()) if now is None else int(now)
    try:
        expires_value = int(expires)
        file_value = int(file_id)
    except (TypeError, ValueError):
        return False
    if expires_value < current:
        return False
    expected = _signature(
        slug=slug,
        file_id=file_value,
        expires=expires_value,
        secret=secret,
    )
    return hmac.compare_digest(signature, expected)
