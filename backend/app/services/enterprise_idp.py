from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from app.config import settings


class EnterpriseIdpAuthenticationError(PermissionError):
    """A safe-to-return authentication failure; it never includes the provider token."""


@dataclass(frozen=True)
class EnterpriseIdpAssertion:
    work_no: str
    issued_at: datetime
    expires_at: datetime
    token_fingerprint: str

    @property
    def expires_at_epoch(self) -> int:
        return int(self.expires_at.timestamp())


class EnterpriseIdpAssertionService:
    """Decrypt and validate the Enterprise SSO AES-CBC login assertion in memory only."""

    def decrypt_and_validate(self, token: str, *, now: datetime | None = None) -> EnterpriseIdpAssertion:
        ciphertext = self._decode_ciphertext(token)
        plaintext = self._decrypt(ciphertext)
        try:
            payload = json.loads(plaintext)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EnterpriseIdpAuthenticationError("invalid EnterpriseIdp assertion") from exc
        if not isinstance(payload, dict):
            raise EnterpriseIdpAuthenticationError("invalid EnterpriseIdp assertion")

        work_no = _normalized_string(payload.get("workNo"))
        raw_time = _normalized_string(payload.get("time"))
        if not work_no or len(work_no) > 32 or not raw_time:
            raise EnterpriseIdpAuthenticationError("invalid EnterpriseIdp assertion")

        timezone = _enterprise_idp_timezone()
        try:
            issued_at = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone)
        except ValueError as exc:
            raise EnterpriseIdpAuthenticationError("invalid EnterpriseIdp assertion time") from exc

        current_time = now.astimezone(timezone) if now else datetime.now(timezone)
        skew = timedelta(seconds=settings.ENTERPRISE_IDP_CLOCK_SKEW_SECONDS)
        provider_expires_at = issued_at + timedelta(seconds=settings.ENTERPRISE_IDP_ASSERTION_MAX_AGE_SECONDS)
        if issued_at > current_time + skew or current_time > provider_expires_at:
            raise EnterpriseIdpAuthenticationError("expired EnterpriseIdp assertion")

        # The platform JWT must never outlive this assertion.  The small margin avoids
        # clients receiving a JWT that expires while a request is in flight.
        jwt_expires_at = provider_expires_at - skew
        if jwt_expires_at <= current_time:
            raise EnterpriseIdpAuthenticationError("expired EnterpriseIdp assertion")
        return EnterpriseIdpAssertion(
            work_no=work_no,
            issued_at=issued_at,
            expires_at=jwt_expires_at,
            token_fingerprint=hashlib.sha256(ciphertext).hexdigest(),
        )

    def _decode_ciphertext(self, raw_token: str) -> bytes:
        token = raw_token.strip().replace(" ", "+")
        if not token or len(token) > 4096:
            raise EnterpriseIdpAuthenticationError("invalid EnterpriseIdp assertion")
        try:
            ciphertext = base64.b64decode(token, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise EnterpriseIdpAuthenticationError("invalid EnterpriseIdp assertion") from exc
        if not ciphertext or len(ciphertext) % 16:
            raise EnterpriseIdpAuthenticationError("invalid EnterpriseIdp assertion")
        return ciphertext

    def _decrypt(self, ciphertext: bytes) -> bytes:
        try:
            key = settings.ENTERPRISE_IDP_AES_KEY.encode("utf-8")
            iv = settings.ENTERPRISE_IDP_AES_IV.encode("utf-8")
            if len(key) != 16 or len(iv) != 16:
                raise ValueError("invalid AES input")
            decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
            padded_plaintext = decryptor.update(ciphertext) + decryptor.finalize()
            unpadder = PKCS7(algorithms.AES.block_size).unpadder()
            return unpadder.update(padded_plaintext) + unpadder.finalize()
        except ValueError as exc:
            raise EnterpriseIdpAuthenticationError("invalid EnterpriseIdp assertion") from exc


def _enterprise_idp_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(settings.ENTERPRISE_IDP_TIMEZONE)
    except ZoneInfoNotFoundError as exc:
        raise EnterpriseIdpAuthenticationError("invalid EnterpriseIdp timezone configuration") from exc


def _normalized_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
