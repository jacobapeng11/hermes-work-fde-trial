from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from app.schemas.runtime_edge import (
    FileTransferTicketClaims,
    RuntimeAccessClaims,
    SessionGrantClaims,
)
from app.schemas.provider_routes import ProviderRouteGrantV1


class RuntimeTicketError(ValueError):
    pass


class RuntimeTicketIssuanceError(RuntimeTicketError):
    pass


class RuntimeTicketIssuer:
    """Issue short Runtime-scoped tickets with the binding's existing API key."""

    def __init__(
        self,
        *,
        runtime_id: str,
        secret: str,
        admission_generation: int | None = None,
        grant_issuance_enabled: bool = True,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("Runtime ticket secret must be at least 32 characters")
        if admission_generation is not None and admission_generation <= 0:
            raise ValueError("admission_generation must be positive")
        self.runtime_id = runtime_id
        self._secret = secret
        self._admission_generation = admission_generation
        self._grant_issuance_enabled = bool(grant_issuance_enabled)

    @property
    def admission_generation(self) -> int | None:
        return self._admission_generation

    def runtime_access(
        self,
        *,
        sub: str,
        user_id: int,
        org_id: int,
        allowed_origin: str,
        connection_nonce: str,
        issued_at: int | None = None,
    ) -> str:
        self._require_grant_issuance()
        now = int(time.time()) if issued_at is None else issued_at
        claim_values: dict[str, Any] = {
            "sub": sub,
            "user_id": user_id,
            "org_id": org_id,
            "runtime_id": self.runtime_id,
            "allowed_origin": allowed_origin,
            "connection_nonce": connection_nonce,
            "issued_at": now,
            "expires_at": now + 300,
        }
        self._add_admission_generation(RuntimeAccessClaims, claim_values)
        claims = RuntimeAccessClaims(**claim_values)
        return self._encode(claims.model_dump(mode="json"))

    def session_grant(
        self,
        *,
        user_id: int,
        org_id: int,
        session_id: str,
        profile_id: str,
        runtime_agent_profile_id: int,
        application_agent_id: int | None = None,
        model_id: str,
        upstream_model_id: str,
        context_length: int = 128_000,
        runtime_capabilities: dict[str, str] | None = None,
        provider_route: ProviderRouteGrantV1 | None = None,
        config_revision: str,
        profile_desired_revision: int,
        profile_applied_revision: int,
        profile_digest: str,
        skill_digest: str,
        mcp_digest: str,
        knowledge_digest: str,
        max_prompt_bytes: int,
        max_output_tokens: int,
        attachments: list[dict[str, str | None]] | None = None,
        issued_at: int | None = None,
    ) -> str:
        self._require_grant_issuance()
        now = int(time.time()) if issued_at is None else issued_at
        claim_values: dict[str, Any] = {
            "user_id": user_id,
            "org_id": org_id,
            "runtime_id": self.runtime_id,
            "session_id": session_id,
            "profile_id": profile_id,
            "runtime_agent_profile_id": runtime_agent_profile_id,
            "application_agent_id": application_agent_id,
            "model_id": model_id,
            "upstream_model_id": upstream_model_id,
            "context_length": context_length,
            "runtime_capabilities": runtime_capabilities or {},
            "provider_route": (
                provider_route.model_dump(mode="json")
                if provider_route is not None
                else None
            ),
            "config_revision": config_revision,
            "profile_desired_revision": profile_desired_revision,
            "profile_applied_revision": profile_applied_revision,
            "profile_digest": profile_digest,
            "skill_digest": skill_digest,
            "mcp_digest": mcp_digest,
            "knowledge_digest": knowledge_digest,
            "max_prompt_bytes": max_prompt_bytes,
            "max_output_tokens": max_output_tokens,
            "attachments": attachments or [],
            "issued_at": now,
            "expires_at": now + 1800,
        }
        self._add_admission_generation(SessionGrantClaims, claim_values)
        claims = SessionGrantClaims(**claim_values)
        return self._encode(claims.model_dump(mode="json"))

    def file_transfer(
        self,
        *,
        operation: str,
        user_id: int,
        org_id: int,
        session_id: str,
        file_id: int,
        relative_path: str,
        display_name: str,
        mime_type: str | None,
        max_bytes: int,
        issued_at: int | None = None,
    ) -> str:
        now = int(time.time()) if issued_at is None else issued_at
        claims = FileTransferTicketClaims(
            ticket_id=secrets.token_urlsafe(24),
            operation=operation,
            user_id=user_id,
            org_id=org_id,
            runtime_id=self.runtime_id,
            session_id=session_id,
            file_id=file_id,
            relative_path=relative_path,
            display_name=display_name,
            mime_type=mime_type,
            max_bytes=max_bytes,
            issued_at=now,
            expires_at=now + 300,
        )
        return self._encode(claims.model_dump(mode="json"))

    def _encode(self, claims: dict[str, Any]) -> str:
        header = {"alg": "HS256", "typ": "JWT"}
        signing_input = f"{_b64json(header)}.{_b64json(claims)}"
        signature = _b64(
            hmac.new(
                self._secret.encode("utf-8"),
                signing_input.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        return f"{signing_input}.{signature}"

    def _require_grant_issuance(self) -> None:
        if not self._grant_issuance_enabled:
            raise RuntimeTicketIssuanceError(
                "RUNTIME_GRANT_ISSUANCE_DISABLED"
            )

    def _add_admission_generation(
        self,
        claims_type: type[RuntimeAccessClaims] | type[SessionGrantClaims],
        values: dict[str, Any],
    ) -> None:
        model_fields = getattr(claims_type, "model_fields", {})
        if "admission_generation" not in model_fields:
            return
        if self._admission_generation is None:
            raise RuntimeTicketIssuanceError(
                "RUNTIME_ADMISSION_GENERATION_REQUIRED"
            )
        values["admission_generation"] = self._admission_generation

    def verify_runtime_access(
        self,
        token: str,
        *,
        expected_origin: str,
        now: int | None = None,
    ) -> RuntimeAccessClaims:
        payload = self._decode(token)
        try:
            claims = RuntimeAccessClaims.model_validate(payload)
        except Exception as exc:
            raise RuntimeTicketError("RuntimeAccess claims are invalid") from exc
        current = int(time.time()) if now is None else now
        if claims.runtime_id != self.runtime_id:
            raise RuntimeTicketError("RuntimeAccess Runtime does not match")
        if claims.allowed_origin.rstrip("/") != expected_origin.rstrip("/"):
            raise RuntimeTicketError("RuntimeAccess Origin does not match")
        if claims.issued_at > current + 30 or claims.expires_at < current:
            raise RuntimeTicketError("RuntimeAccess has expired")
        return claims

    def verify_file_transfer(
        self,
        token: str,
        *,
        operation: str,
        now: int | None = None,
    ) -> FileTransferTicketClaims:
        payload = self._decode(token)
        try:
            claims = FileTransferTicketClaims.model_validate(payload)
        except Exception as exc:
            raise RuntimeTicketError(
                "FileTransferTicket claims are invalid"
            ) from exc
        current = int(time.time()) if now is None else now
        if claims.runtime_id != self.runtime_id:
            raise RuntimeTicketError(
                "FileTransferTicket Runtime does not match"
            )
        if claims.operation != operation:
            raise RuntimeTicketError(
                "FileTransferTicket operation does not match"
            )
        if claims.issued_at > current + 30 or claims.expires_at < current:
            raise RuntimeTicketError("FileTransferTicket has expired")
        return claims

    def _decode(self, token: str) -> dict[str, Any]:
        try:
            header_part, payload_part, signature_part = token.split(".", 2)
        except ValueError as exc:
            raise RuntimeTicketError("Runtime ticket is malformed") from exc
        signing_input = f"{header_part}.{payload_part}"
        expected = _b64(
            hmac.new(
                self._secret.encode("utf-8"),
                signing_input.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(signature_part, expected):
            raise RuntimeTicketError("Runtime ticket signature is invalid")
        try:
            header = json.loads(_b64decode(header_part))
            payload = json.loads(_b64decode(payload_part))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeTicketError("Runtime ticket is malformed") from exc
        if (
            not isinstance(header, dict)
            or header != {"alg": "HS256", "typ": "JWT"}
            or not isinstance(payload, dict)
        ):
            raise RuntimeTicketError("Runtime ticket header is invalid")
        return payload


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
