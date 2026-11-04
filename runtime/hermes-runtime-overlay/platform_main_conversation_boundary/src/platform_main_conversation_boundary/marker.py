"""Pure construction of ``PlatformMainBoundaryMarkerV1``.

The bootstrap secret is deliberately read from one fixed, inherited file
descriptor.  It is never accepted from argv, environment, config, or disk.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
import re
from typing import Any


BOOTSTRAP_FD = 198
BOOTSTRAP_MAX_BYTES = 4096
HEADER_NAME = "X-Hermes-Platform-Main-Boundary"
HEADER_MAX_BYTES = 4096
MARKER_CONTRACT_VERSION = 1
MARKER_LIFETIME_SECONDS = 20

_ROUTE_BASE_URL_RE = re.compile(
    r"http://127\.0\.0\.1:9121/model-relay/([0-9a-f]{64})/v1"
)
_BASE64URL_RE = re.compile(r"[A-Za-z0-9_-]+")
_MARKER_CONTEXT_FIELDS = (
    "session_id",
    "task_id",
    "turn_id",
    "api_request_id",
)


class BoundaryBootstrapError(RuntimeError):
    """The one-shot marker bootstrap descriptor is missing or malformed."""


def canonical_json(value: object) -> bytes:
    """Encode the marker's closed JSON domain using its RFC 8785 projection.

    Marker field names are fixed ASCII strings and its sole numeric field is a
    non-negative integer, so the standard encoder is byte-equivalent to JCS
    after the checks performed by :meth:`BoundaryMarkerSigner.build_marker`.
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _base64url_no_pad(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(value: object, *, expected_bytes: int, label: str) -> bytes:
    if not isinstance(value, str) or not _BASE64URL_RE.fullmatch(value):
        raise BoundaryBootstrapError(f"{label} must be unpadded base64url")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise BoundaryBootstrapError(f"{label} must be unpadded base64url") from exc
    if len(decoded) != expected_bytes or _base64url_no_pad(decoded) != value:
        raise BoundaryBootstrapError(f"{label} has an invalid length or encoding")
    return decoded


def route_token_from_base_url(value: object) -> str | None:
    """Extract a route token only from the canonical localhost Relay URL."""

    if not isinstance(value, str):
        return None
    matched = _ROUTE_BASE_URL_RE.fullmatch(value)
    return matched.group(1) if matched is not None else None


def _utc_milliseconds(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    milliseconds = normalized.microsecond // 1000
    return normalized.strftime("%Y-%m-%dT%H:%M:%S.") + f"{milliseconds:03d}Z"


def _read_and_close_fixed_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    try:
        while True:
            chunk = os.read(fd, min(1024, BOOTSTRAP_MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > BOOTSTRAP_MAX_BYTES:
                raise BoundaryBootstrapError("marker bootstrap exceeds size limit")
    except OSError as exc:
        raise BoundaryBootstrapError("marker bootstrap descriptor is unavailable") from exc
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return b"".join(chunks)


class BoundaryMarkerSigner:
    """Create one authenticated marker without owning any network behavior."""

    def __init__(
        self,
        *,
        key: bytes,
        boundary_epoch_nonce: str,
        clock: Callable[[], datetime] | None = None,
        random_bytes: Callable[[int], bytes] | None = None,
    ) -> None:
        if len(key) != 32:
            raise BoundaryBootstrapError("marker key must contain exactly 256 bits")
        _decode_base64url(
            boundary_epoch_nonce,
            expected_bytes=16,
            label="boundary_epoch_nonce",
        )
        self._key = bytes(key)
        self.boundary_epoch_nonce = boundary_epoch_nonce
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._random_bytes = random_bytes or os.urandom

    @classmethod
    def from_bootstrap_bytes(
        cls,
        value: bytes,
        *,
        clock: Callable[[], datetime] | None = None,
        random_bytes: Callable[[int], bytes] | None = None,
    ) -> "BoundaryMarkerSigner":
        try:
            payload = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BoundaryBootstrapError("marker bootstrap is not valid JSON") from exc
        expected_fields = {
            "contract_version",
            "algorithm",
            "key_base64url",
            "boundary_epoch_nonce",
        }
        if not isinstance(payload, dict) or set(payload) != expected_fields:
            raise BoundaryBootstrapError("marker bootstrap fields do not match contract")
        if payload.get("contract_version") != MARKER_CONTRACT_VERSION:
            raise BoundaryBootstrapError("marker bootstrap contract version mismatch")
        if payload.get("algorithm") != "hmac-sha256":
            raise BoundaryBootstrapError("marker bootstrap algorithm mismatch")
        key = _decode_base64url(
            payload.get("key_base64url"),
            expected_bytes=32,
            label="key_base64url",
        )
        epoch = payload.get("boundary_epoch_nonce")
        if not isinstance(epoch, str):
            raise BoundaryBootstrapError("boundary_epoch_nonce must be a string")
        return cls(
            key=key,
            boundary_epoch_nonce=epoch,
            clock=clock,
            random_bytes=random_bytes,
        )

    @classmethod
    def from_fixed_fd(cls, fd: int = BOOTSTRAP_FD) -> "BoundaryMarkerSigner":
        return cls.from_bootstrap_bytes(_read_and_close_fixed_fd(fd))

    def build_marker(
        self,
        *,
        route_token: str,
        session_id: str,
        task_id: str,
        turn_id: str,
        api_request_id: str,
        api_call_count: int,
    ) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", route_token) is None:
            raise ValueError("route_token must be 64 lowercase hexadecimal characters")
        identities = (session_id, task_id, turn_id, api_request_id)
        if not all(isinstance(item, str) and item for item in identities):
            raise ValueError("marker identity fields must be non-empty strings")
        if (
            isinstance(api_call_count, bool)
            or not isinstance(api_call_count, int)
            or api_call_count < 0
            or api_call_count > 9_007_199_254_740_991
        ):
            raise ValueError("api_call_count must be a non-negative JCS-safe integer")
        issued = self._clock().astimezone(timezone.utc)
        expires = issued + timedelta(seconds=MARKER_LIFETIME_SECONDS)
        request_nonce = _base64url_no_pad(self._random_bytes(16))
        _decode_base64url(request_nonce, expected_bytes=16, label="request_nonce")
        payload = {
            "contract_version": MARKER_CONTRACT_VERSION,
            "route_token": route_token,
            "session_id": session_id,
            "task_id": task_id,
            "turn_id": turn_id,
            "api_request_id": api_request_id,
            "api_call_count": api_call_count,
            "boundary_epoch_nonce": self.boundary_epoch_nonce,
            "request_nonce": request_nonce,
            "issued_at": _utc_milliseconds(issued),
            "expires_at": _utc_milliseconds(expires),
        }
        encoded = canonical_json(payload)
        signature = hmac.new(self._key, encoded, hashlib.sha256).hexdigest()
        header = f"{_base64url_no_pad(encoded)}.{signature}"
        if len(header.encode("ascii")) > HEADER_MAX_BYTES:
            raise ValueError("boundary marker exceeds header size limit")
        return header

    def apply(self, request: object, **context: Any) -> dict[str, Any] | None:
        """Hermes ``llm_request`` middleware callback.

        Invalid or non-Relay contexts are normal unmarked requests.  The
        callback never opens sockets, starts processes, registers tools, or
        changes the request body.
        """

        if not isinstance(request, dict):
            return None
        route_token = route_token_from_base_url(context.get("base_url"))
        if route_token is None:
            return None
        identity: dict[str, str] = {}
        for field in _MARKER_CONTEXT_FIELDS:
            value = context.get(field)
            if not isinstance(value, str) or not value:
                return None
            identity[field] = value
        api_call_count = context.get("api_call_count")
        try:
            marker = self.build_marker(
                route_token=route_token,
                api_call_count=api_call_count,
                **identity,
            )
        except ValueError:
            return None
        raw_headers = request.get("extra_headers")
        if raw_headers is not None and not isinstance(raw_headers, Mapping):
            return None
        headers = {
            str(key): value
            for key, value in dict(raw_headers or {}).items()
            if str(key).lower() != HEADER_NAME.lower()
        }
        headers[HEADER_NAME] = marker
        rewritten = dict(request)
        rewritten["extra_headers"] = headers
        return {
            "request": rewritten,
            "source": "platform_main_conversation_boundary_v1",
        }
