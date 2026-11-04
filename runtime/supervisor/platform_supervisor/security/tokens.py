from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from platform_supervisor.contracts import (
    FileTransferTicketClaims,
    RuntimeAccessClaims,
    SessionGrantClaims,
)


ClaimsModel = TypeVar("ClaimsModel", bound=BaseModel)


class RuntimeTokenError(RuntimeError):
    pass


class RuntimeAccessReplay(RuntimeTokenError):
    pass


class RuntimeTokenVerifier:
    """Verify Runtime-scoped HS256 tickets using the existing per-Runtime key."""

    def __init__(
        self,
        *,
        runtime_id: str,
        secret: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("Runtime ticket secret must be at least 32 characters")
        self.runtime_id = runtime_id
        self._secret = secret.encode("utf-8")
        self._clock = clock
        self._used_nonces: dict[str, int] = {}
        self._nonce_lock = asyncio.Lock()

    async def verify_runtime_access(
        self,
        token: str,
        *,
        origin: str,
    ) -> RuntimeAccessClaims:
        claims = self._decode(token, RuntimeAccessClaims)
        self._validate_common(claims.runtime_id, claims.expires_at)
        if not hmac.compare_digest(claims.allowed_origin, origin):
            raise RuntimeTokenError("RuntimeAccess Origin does not match")
        async with self._nonce_lock:
            now = int(self._clock())
            self._used_nonces = {
                nonce: expiry
                for nonce, expiry in self._used_nonces.items()
                if expiry >= now
            }
            if claims.connection_nonce in self._used_nonces:
                raise RuntimeAccessReplay("RuntimeAccess has already been consumed")
            self._used_nonces[claims.connection_nonce] = claims.expires_at
        return claims

    def verify_session_grant(self, token: str) -> SessionGrantClaims:
        claims = self._decode(token, SessionGrantClaims)
        self._validate_common(claims.runtime_id, claims.expires_at)
        return claims

    def verify_file_transfer(
        self,
        token: str,
        *,
        operation: str,
    ) -> FileTransferTicketClaims:
        claims = self._decode(token, FileTransferTicketClaims)
        self._validate_common(claims.runtime_id, claims.expires_at)
        if not hmac.compare_digest(claims.operation, operation):
            raise RuntimeTokenError(
                "FileTransferTicket operation does not match"
            )
        return claims

    def _decode(
        self,
        token: str,
        model: type[ClaimsModel],
    ) -> ClaimsModel:
        try:
            header_part, payload_part, signature_part = token.split(".", 2)
        except ValueError as exc:
            raise RuntimeTokenError("Runtime ticket is malformed") from exc
        signing_input = f"{header_part}.{payload_part}"
        expected = _b64(
            hmac.new(
                self._secret,
                signing_input.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(signature_part, expected):
            raise RuntimeTokenError("Runtime ticket signature is invalid")
        try:
            header = json.loads(_b64decode(header_part))
            payload = json.loads(_b64decode(payload_part))
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise RuntimeTokenError("Runtime ticket payload is invalid") from exc
        if header != {"alg": "HS256", "typ": "JWT"}:
            raise RuntimeTokenError("Runtime ticket header is invalid")
        if not isinstance(payload, dict):
            raise RuntimeTokenError("Runtime ticket payload is not an object")
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            raise RuntimeTokenError("Runtime ticket claims are invalid") from exc

    def _validate_common(self, runtime_id: str, expires_at: int) -> None:
        if not hmac.compare_digest(runtime_id, self.runtime_id):
            raise RuntimeTokenError("Runtime ticket belongs to another Runtime")
        if expires_at < int(self._clock()):
            raise RuntimeTokenError("Runtime ticket has expired")


def encode_runtime_token(claims: dict[str, Any], *, secret: str) -> str:
    if len(secret) < 32:
        raise ValueError("Runtime ticket secret must be at least 32 characters")
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = f"{_b64json(header)}.{_b64json(claims)}"
    signature = _b64(
        hmac.new(
            secret.encode("utf-8"),
            signing_input.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )
    return f"{signing_input}.{signature}"


def _b64json(value: dict[str, Any]) -> str:
    return _b64(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
