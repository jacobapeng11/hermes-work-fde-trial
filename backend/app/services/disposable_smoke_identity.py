"""Create short-lived smoke identities with mandatory, verified cleanup."""

from __future__ import annotations

import json
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Awaitable, Callable, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import User


_PURPOSE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,11}[a-z0-9])?$")
_SMOKE_CODE = re.compile(r"^smoke-[a-z0-9][a-z0-9-]{0,12}-[0-9a-f]{8}$")


class SmokeIdentityMismatch(RuntimeError):
    """Raised when cleanup no longer points at the identity that was created."""


@dataclass(frozen=True)
class SmokeIdentityManifest:
    org_id: int
    user_id: int
    user_code: str
    purpose: str
    created_at: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


class SmokeIdentityStore(Protocol):
    async def create(self, *, purpose: str, user_code: str) -> SmokeIdentityManifest:
        ...

    async def delete(self, manifest: SmokeIdentityManifest) -> None:
        ...


class SqlAlchemySmokeIdentityStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    ) -> None:
        self.session_factory = session_factory

    async def create(self, *, purpose: str, user_code: str) -> SmokeIdentityManifest:
        async with self.session_factory() as db:
            user = User(
                org_id=settings.DEFAULT_ORG_ID,
                user_code=user_code,
                name=f"Disposable smoke: {purpose}",
                status="active",
                sso_subject=user_code,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return SmokeIdentityManifest(
                org_id=int(user.org_id),
                user_id=int(user.id),
                user_code=str(user.user_code),
                purpose=purpose,
                created_at=datetime.now(timezone.utc).isoformat(),
            )

    async def delete(self, manifest: SmokeIdentityManifest) -> None:
        if not _SMOKE_CODE.fullmatch(manifest.user_code):
            raise SmokeIdentityMismatch(
                f"cleanup refused a non-disposable user code: {manifest.user_code!r}"
            )
        async with self.session_factory() as db:
            async with db.begin():
                user = (
                    await db.execute(
                        select(User)
                        .where(User.id == manifest.user_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    user is None
                    or int(user.org_id) != manifest.org_id
                    or str(user.user_code) != manifest.user_code
                ):
                    raise SmokeIdentityMismatch(
                        "cleanup identity no longer matches its creation manifest"
                    )
                await db.delete(user)


Cleanup = Callable[[], Awaitable[None]]


class DisposableSmokeIdentity:
    def __init__(self, manifest: SmokeIdentityManifest) -> None:
        self.manifest = manifest
        self._cleanup_callbacks: list[Cleanup] = []

    def add_cleanup(self, callback: Cleanup) -> None:
        self._cleanup_callbacks.append(callback)

    async def cleanup_resources(self) -> None:
        for callback in reversed(self._cleanup_callbacks):
            await callback()


@asynccontextmanager
async def disposable_smoke_identity(
    purpose: str,
    *,
    store: SmokeIdentityStore | None = None,
) -> AsyncIterator[DisposableSmokeIdentity]:
    normalized = purpose.strip().lower()
    if not _PURPOSE.fullmatch(normalized):
        raise ValueError(
            "smoke purpose must be 1-13 lowercase letters, digits, or internal hyphens"
        )
    identity_store = store or SqlAlchemySmokeIdentityStore()
    user_code = f"smoke-{normalized}-{secrets.token_hex(4)}"
    manifest = await identity_store.create(
        purpose=normalized,
        user_code=user_code,
    )
    if manifest.user_code != user_code:
        raise SmokeIdentityMismatch("created identity does not match requested user code")
    identity = DisposableSmokeIdentity(manifest)
    try:
        yield identity
    finally:
        await identity.cleanup_resources()
        await identity_store.delete(manifest)
