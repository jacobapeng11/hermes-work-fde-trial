from __future__ import annotations

import base64
import binascii
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings


class ManagedSecretError(ValueError):
    pass


class ManagedSecretCodec:
    prefix = "aesgcm-v1:"

    def __init__(self, key_material: str | None = None) -> None:
        material = (
            key_material
            if key_material is not None
            else settings.AES_ENCRYPTION_KEY
        )
        if not material:
            raise ManagedSecretError("managed secret key is not configured")
        self._key = hashlib.sha256(material.encode("utf-8")).digest()

    def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._key).encrypt(nonce, plaintext.encode("utf-8"), None)
        payload = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return f"{self.prefix}{payload}"

    def decrypt(self, encrypted: str) -> str:
        if not encrypted.startswith(self.prefix):
            raise ManagedSecretError("unsupported managed secret format")
        try:
            payload = base64.urlsafe_b64decode(
                encrypted[len(self.prefix) :].encode("ascii")
            )
            nonce, ciphertext = payload[:12], payload[12:]
            if len(nonce) != 12 or not ciphertext:
                raise ValueError("invalid encrypted payload")
            plaintext = AESGCM(self._key).decrypt(nonce, ciphertext, None)
            return plaintext.decode("utf-8")
        except (ValueError, binascii.Error, InvalidTag, UnicodeDecodeError) as exc:
            raise ManagedSecretError("managed secret cannot be decrypted") from exc
