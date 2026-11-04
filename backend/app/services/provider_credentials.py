from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class ProviderCredentialCodec:
    """Encrypt Provider API keys at rest and keep legacy values readable."""

    prefix = "provider-v1:"

    def __init__(self, secret: str | None = None) -> None:
        material = (secret or settings.AES_ENCRYPTION_KEY).encode("utf-8")
        key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
        self.fernet = Fernet(key)

    def encrypt(self, value: str) -> str:
        if not value:
            return ""
        return self.prefix + self.fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not value.startswith(self.prefix):
            return value
        token = value[len(self.prefix) :]
        try:
            return self.fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
            raise ValueError("provider credential cannot be decrypted") from exc
