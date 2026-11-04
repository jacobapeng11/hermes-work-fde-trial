from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RuntimeBootstrapIntent


RuntimeBindingType = Literal["personal_runtime", "application_runtime"]


class RuntimeBootstrapIntentError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeBootstrapAuthorization:
    intent_id: str
    authorization_digest: str
    status: str
    document: dict[str, object]


def runtime_bootstrap_authorization_digest(document: dict[str, object]) -> str:
    payload = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class RuntimeBootstrapIntentService:
    """Create or reuse the exact authorization for one fresh bootstrap."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def authorize(
        self,
        *,
        binding_type: RuntimeBindingType,
        binding_id: int,
        runtime_id: str,
        pvc_storage_identity_hash: str,
        target_image: str,
        target_image_digest: str,
        authorized_at: datetime | None = None,
    ) -> RuntimeBootstrapAuthorization:
        if binding_type not in {"personal_runtime", "application_runtime"}:
            raise ValueError("unsupported Runtime binding type")
        if int(binding_id) <= 0:
            raise ValueError("binding_id must be positive")
        identity = {
            "contract_version": "runtime-bootstrap-intent-v1",
            "runtime_binding_type": binding_type,
            "runtime_binding_id": int(binding_id),
            "runtime_id": self._required(runtime_id, "runtime_id"),
            "pvc_storage_identity_hash": self._required(
                pvc_storage_identity_hash, "pvc_storage_identity_hash"
            ),
            "target_image": self._required(target_image, "target_image"),
            "target_image_digest": self._required(
                target_image_digest, "target_image_digest"
            ),
        }
        digest = runtime_bootstrap_authorization_digest(identity)
        existing = await self.db.scalar(
            select(RuntimeBootstrapIntent).where(
                RuntimeBootstrapIntent.authorization_digest == digest
            )
        )
        if existing is not None:
            if dict(existing.authorization_document or {}) != identity:
                raise RuntimeBootstrapIntentError(
                    "bootstrap authorization digest collision"
                )
            return self._authorization(existing)

        conflicting = await self.db.scalar(
            select(RuntimeBootstrapIntent)
            .where(
                RuntimeBootstrapIntent.runtime_binding_type == binding_type,
                RuntimeBootstrapIntent.runtime_binding_id == int(binding_id),
                RuntimeBootstrapIntent.status.in_(("authorized", "completed")),
            )
            .order_by(RuntimeBootstrapIntent.id.desc())
            .limit(1)
        )
        if conflicting is not None:
            raise RuntimeBootstrapIntentError(
                "a different bootstrap authorization already exists for binding"
            )

        row = RuntimeBootstrapIntent(
            intent_id=str(uuid4()),
            runtime_binding_type=binding_type,
            runtime_binding_id=int(binding_id),
            runtime_id=str(identity["runtime_id"]),
            pvc_storage_identity_hash=str(identity["pvc_storage_identity_hash"]),
            target_image=str(identity["target_image"]),
            target_image_digest=str(identity["target_image_digest"]),
            authorization_digest=digest,
            authorization_document=identity,
            status="authorized",
            authorized_at=authorized_at or datetime.utcnow(),
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return self._authorization(row)

    async def mark_completed(
        self, *, intent_id: str, completed_at: datetime | None = None
    ) -> RuntimeBootstrapAuthorization:
        row = await self._locked(intent_id)
        if row.status == "failed":
            raise RuntimeBootstrapIntentError("failed bootstrap intent cannot complete")
        row.status = "completed"
        row.completed_at = completed_at or datetime.utcnow()
        row.last_error = None
        await self.db.commit()
        return self._authorization(row)

    async def mark_failed(
        self, *, intent_id: str, error_summary: str
    ) -> RuntimeBootstrapAuthorization:
        row = await self._locked(intent_id)
        if row.status == "completed":
            raise RuntimeBootstrapIntentError("completed bootstrap intent cannot fail")
        row.status = "failed"
        row.last_error = " ".join(str(error_summary).split())[:1000]
        await self.db.commit()
        return self._authorization(row)

    async def _locked(self, intent_id: str) -> RuntimeBootstrapIntent:
        row = (
            await self.db.execute(
                select(RuntimeBootstrapIntent)
                .where(
                    RuntimeBootstrapIntent.intent_id
                    == self._required(intent_id, "intent_id")
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise RuntimeBootstrapIntentError("bootstrap intent not found")
        return row

    @staticmethod
    def _authorization(row: RuntimeBootstrapIntent) -> RuntimeBootstrapAuthorization:
        return RuntimeBootstrapAuthorization(
            intent_id=str(row.intent_id),
            authorization_digest=str(row.authorization_digest),
            status=str(row.status),
            document=dict(row.authorization_document or {}),
        )

    @staticmethod
    def _required(value: object, field: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{field} must not be empty")
        return normalized


__all__ = [
    "RuntimeBootstrapAuthorization",
    "RuntimeBootstrapIntentError",
    "RuntimeBootstrapIntentService",
    "runtime_bootstrap_authorization_digest",
]
