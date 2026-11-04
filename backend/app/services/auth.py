from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import User
from app.services.admin_auth import AdminAuthorizationError, AdminAuthorizationService
from app.services.runtime_workspace import Principal


class AuthenticationError(PermissionError):
    pass


@dataclass(frozen=True)
class AuthenticatedUser:
    org_id: int
    user_id: int
    name: str
    email: str | None
    user_code: str | None


@dataclass(frozen=True)
class LoginCommand:
    identifier: str
    password: str
    org_code: str = "default"


class AuthUserStore(Protocol):
    async def authenticate(self, command: LoginCommand) -> AuthenticatedUser:
        ...

    async def get_user(self, *, org_id: int, user_id: int) -> AuthenticatedUser:
        ...


class AuthTokenService:
    def __init__(self, store: AuthUserStore) -> None:
        self.store = store

    async def login(self, command: LoginCommand) -> tuple[str, AuthenticatedUser]:
        user = await self.store.authenticate(command)
        return create_access_token(Principal(org_id=user.org_id, user_id=user.user_id)), user

    async def principal_from_bearer(self, authorization: str | None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthenticationError("missing bearer token")
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            raise AuthenticationError("missing bearer token")
        principal = decode_access_token(token)
        await self.store.get_user(org_id=principal.org_id, user_id=principal.user_id)
        return principal


class AdminPasswordAuthStore(Protocol):
    async def authenticate_admin(self, *, username: str, password: str) -> AuthenticatedUser:
        ...


class AdminPasswordLoginService:
    """Authenticate only the fixed password-mode administrator account."""

    def __init__(
        self,
        store: AdminPasswordAuthStore,
        authorization: AdminAuthorizationService,
    ) -> None:
        self.store = store
        self.authorization = authorization

    async def login(self, *, username: str, password: str) -> tuple[str, AuthenticatedUser]:
        user = await self.store.authenticate_admin(username=username, password=password)
        try:
            await self.authorization.require_admin(
                Principal(org_id=user.org_id, user_id=user.user_id)
            )
        except AdminAuthorizationError as exc:
            raise AuthenticationError("invalid credentials") from exc
        return create_access_token(Principal(org_id=user.org_id, user_id=user.user_id)), user


class InMemoryAuthUserStore:
    def __init__(self, user: AuthenticatedUser | None = None) -> None:
        dev_user = user or AuthenticatedUser(
            org_id=settings.DEFAULT_ORG_ID,
            user_id=settings.DEV_PRINCIPAL_USER_ID,
            name="Development User",
            email=None,
            user_code="dev-user",
        )
        self.users = {
            "dev-user": dev_user,
            "admin": dev_user,
        }
        self.passwords = {
            "dev-user": "dev-user",
            "admin": "admin",
        }

    async def authenticate(self, command: LoginCommand) -> AuthenticatedUser:
        identifier = command.identifier.strip().lower()
        if self.passwords.get(identifier) != command.password:
            raise AuthenticationError("invalid credentials")
        return self.users[identifier]

    async def get_user(self, *, org_id: int, user_id: int) -> AuthenticatedUser:
        for user in self.users.values():
            if user.org_id == org_id and user.user_id == user_id:
                return user
        raise AuthenticationError("user not found")


class SqlAlchemyAuthUserStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def authenticate(self, command: LoginCommand) -> AuthenticatedUser:
        identifier = command.identifier.strip()
        result = await self.db.execute(
            select(User).where(
                User.status == "active",
                or_(User.email == identifier, User.user_code == identifier),
            )
        )
        user = result.scalars().first()
        if user is None or not verify_password(command.password, user.password_hash):
            raise AuthenticationError("invalid credentials")
        return _authenticated_user(user)

    async def get_user(self, *, org_id: int, user_id: int) -> AuthenticatedUser:
        result = await self.db.execute(
            select(User).where(
                User.org_id == org_id,
                User.id == user_id,
                User.status == "active",
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise AuthenticationError("user not found")
        return _authenticated_user(user)


class SqlAlchemyAdminPasswordAuthStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def authenticate_admin(self, *, username: str, password: str) -> AuthenticatedUser:
        if username != "admin":
            raise AuthenticationError("invalid credentials")
        result = await self.db.execute(
            select(User).where(
                User.org_id == settings.DEFAULT_ORG_ID,
                User.user_code == "admin",
                User.status == "active",
            )
        )
        user = result.scalar_one_or_none()
        if user is None or not verify_password(password, user.password_hash):
            raise AuthenticationError("invalid credentials")
        return _authenticated_user(user)

def hash_password(password: str, *, salt: str | None = None, iterations: int = 260000) -> str:
    next_salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), next_salt.encode("utf-8"), iterations)
    return f"pbkdf2_sha256${iterations}${next_salt}${base64.urlsafe_b64encode(digest).decode('ascii')}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        algorithm, raw_iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hash_password(password, salt=salt, iterations=int(raw_iterations)).split("$", 3)[3]
        return hmac.compare_digest(candidate, expected)
    except (ValueError, TypeError):
        return False


def is_password_hash_usable(encoded: str | None) -> bool:
    """Check the stored PBKDF2 representation without needing its plaintext."""
    if not encoded:
        return False
    try:
        algorithm, raw_iterations, salt, digest = encoded.split("$", 3)
        iterations = int(raw_iterations)
        decoded = base64.b64decode(
            digest + "=" * (-len(digest) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError):
        return False
    return (
        algorithm == "pbkdf2_sha256"
        and bool(salt)
        and len(decoded) == hashlib.sha256().digest_size
        and 1 <= iterations <= 10_000_000
    )


def create_access_token(
    principal: Principal,
    *,
    issued_at: int | None = None,
    expires_at: int | None = None,
) -> str:
    now = issued_at if issued_at is not None else int(time.time())
    expires = expires_at if expires_at is not None else now + settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60
    if expires <= now:
        raise AuthenticationError("access token expiration must be in the future")
    payload = {
        "sub": str(principal.user_id),
        "org_id": principal.org_id,
        "user_id": principal.user_id,
        "token_type": "access",
        "iat": now,
        "exp": expires,
    }
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = f"{_b64json(header)}.{_b64json(payload)}"
    signature = _b64(hmac.new(settings.JWT_SECRET_KEY.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256).digest())
    return f"{signing_input}.{signature}"


def decode_access_token(token: str) -> Principal:
    try:
        header_part, payload_part, signature_part = token.split(".", 2)
    except ValueError as exc:
        raise AuthenticationError("invalid bearer token") from exc
    signing_input = f"{header_part}.{payload_part}"
    expected = _b64(hmac.new(settings.JWT_SECRET_KEY.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature_part, expected):
        raise AuthenticationError("invalid bearer token")
    try:
        payload = json.loads(_b64decode(payload_part))
    except (ValueError, TypeError) as exc:
        raise AuthenticationError("invalid bearer token") from exc
    if payload.get("token_type") != "access":
        raise AuthenticationError("invalid bearer token")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise AuthenticationError("expired bearer token")
    return Principal(org_id=int(payload["org_id"]), user_id=int(payload["user_id"]))


def _authenticated_user(user: User) -> AuthenticatedUser:
    return AuthenticatedUser(
        org_id=int(user.org_id),
        user_id=int(user.id),
        name=str(user.name),
        email=str(user.email) if user.email else None,
        user_code=str(user.user_code) if user.user_code else None,
    )


def _b64json(value: dict) -> str:
    return _b64(json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
