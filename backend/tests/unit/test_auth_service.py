from __future__ import annotations

import pytest

from app.config import settings
from app.services.auth import (
    AuthTokenService,
    AuthenticationError,
    InMemoryAuthUserStore,
    LoginCommand,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_password_hash_verification_uses_pbkdf2() -> None:
    encoded = hash_password("correct", salt="fixed-salt", iterations=1000)

    assert encoded.startswith("pbkdf2_sha256$")
    assert verify_password("correct", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("correct", None)


@pytest.mark.asyncio
async def test_auth_service_issues_and_validates_jwt_principal() -> None:
    service = AuthTokenService(InMemoryAuthUserStore())

    token, user = await service.login(LoginCommand(identifier="dev-user", password="dev-user"))
    principal = decode_access_token(token)

    assert user.user_id == settings.DEV_PRINCIPAL_USER_ID
    assert principal.user_id == settings.DEV_PRINCIPAL_USER_ID
    assert principal.org_id == 1


def test_access_token_rejects_non_access_token_type() -> None:
    from app.services import auth

    original = auth.settings.JWT_SECRET_KEY
    auth.settings.JWT_SECRET_KEY = "test-secret"
    try:
        token = auth.create_access_token(
            auth.Principal(org_id=1, user_id=1),
        )
        header, payload, signature = token.split(".")
        import base64
        import json

        decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        decoded["token_type"] = "refresh"
        replacement = base64.urlsafe_b64encode(
            json.dumps(decoded, separators=(",", ":"), sort_keys=True).encode()
        ).decode().rstrip("=")
        signing_input = f"{header}.{replacement}"
        signature = auth._b64(
            auth.hmac.new(
                auth.settings.JWT_SECRET_KEY.encode(),
                signing_input.encode(),
                auth.hashlib.sha256,
            ).digest()
        )
        with pytest.raises(auth.AuthenticationError, match="invalid bearer token"):
            auth.decode_access_token(f"{signing_input}.{signature}")
    finally:
        auth.settings.JWT_SECRET_KEY = original


@pytest.mark.asyncio
async def test_auth_service_rejects_invalid_credentials() -> None:
    service = AuthTokenService(InMemoryAuthUserStore())

    with pytest.raises(AuthenticationError):
        await service.login(LoginCommand(identifier="dev-user", password="bad"))
