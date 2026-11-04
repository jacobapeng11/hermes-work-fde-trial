from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RuntimeMaintenanceControl


MAINTENANCE_STATE_OPEN = "open"
MAINTENANCE_STATE_FROZEN = "frozen"
_EPOCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


class RuntimeMaintenanceError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        detail: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.retryable = retryable


@dataclass(frozen=True)
class RuntimeMaintenanceSnapshot:
    maintenance_state: str
    maintenance_epoch: str | None
    maintenance_reason: str | None
    started_at: datetime | None
    completed_at: datetime | None
    admission_generation: int
    grant_issuance_enabled: bool

    @property
    def maintenance_active(self) -> bool:
        return self.maintenance_state == MAINTENANCE_STATE_FROZEN

    @property
    def credential_admission_generation(self) -> int:
        if self.maintenance_active or not self.grant_issuance_enabled:
            raise RuntimeMaintenanceError(
                "RUNTIME_GRANT_ISSUANCE_DISABLED",
                detail="global Runtime maintenance is frozen",
                retryable=True,
            )
        return self.admission_generation


class RuntimeMaintenanceService:
    """Fail-closed singleton gate for Runtime creation, work and grants."""

    CONTROL_ID = 1

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def snapshot(self, *, lock: bool = False) -> RuntimeMaintenanceSnapshot:
        statement = select(RuntimeMaintenanceControl).where(
            RuntimeMaintenanceControl.id == self.CONTROL_ID
        )
        if lock:
            statement = statement.with_for_update()
        row = (await self.db.execute(statement)).scalar_one_or_none()
        if row is None:
            raise RuntimeMaintenanceError(
                "RUNTIME_MAINTENANCE_CONTROL_UNINITIALIZED",
                detail="runtime_maintenance_control singleton row is missing",
            )
        return self._validated_snapshot(row)

    async def require_admission_open(
        self, *, lock: bool = False
    ) -> RuntimeMaintenanceSnapshot:
        snapshot = await self.snapshot(lock=lock)
        if snapshot.maintenance_active:
            raise RuntimeMaintenanceError(
                "MAINTENANCE_ADMISSION_FROZEN",
                detail="global Runtime maintenance is active",
                retryable=True,
            )
        if not snapshot.grant_issuance_enabled:
            raise RuntimeMaintenanceError(
                "RUNTIME_MAINTENANCE_CONTROL_INVALID",
                detail="open maintenance state cannot disable grants",
            )
        return snapshot

    async def require_grant_issuance(
        self, *, lock: bool = False
    ) -> RuntimeMaintenanceSnapshot:
        snapshot = await self.require_admission_open(lock=lock)
        snapshot.credential_admission_generation
        return snapshot

    async def freeze(
        self,
        *,
        maintenance_epoch: str,
        reason: str,
        now: datetime | None = None,
    ) -> RuntimeMaintenanceSnapshot:
        epoch = self._epoch(maintenance_epoch)
        cleaned_reason = " ".join(str(reason).split())[:500]
        if not cleaned_reason:
            raise ValueError("maintenance reason must not be empty")
        row = await self._locked_row()
        snapshot = self._validated_snapshot(row)
        if snapshot.maintenance_active:
            if snapshot.maintenance_epoch != epoch:
                raise RuntimeMaintenanceError(
                    "RUNTIME_MAINTENANCE_IDENTITY_CONFLICT",
                    detail="a different maintenance epoch is already active",
                )
            await self.db.commit()
            return snapshot
        row.maintenance_state = MAINTENANCE_STATE_FROZEN
        row.maintenance_epoch = epoch
        row.maintenance_reason = cleaned_reason
        row.started_at = now or datetime.utcnow()
        row.completed_at = None
        row.grant_issuance_enabled = False
        return await self._commit(row)

    async def open(
        self,
        *,
        maintenance_epoch: str,
        now: datetime | None = None,
    ) -> RuntimeMaintenanceSnapshot:
        epoch = self._epoch(maintenance_epoch)
        row = await self._locked_row()
        snapshot = self._validated_snapshot(row)
        if not snapshot.maintenance_active:
            if snapshot.maintenance_epoch != epoch:
                raise RuntimeMaintenanceError(
                    "RUNTIME_MAINTENANCE_IDENTITY_CONFLICT",
                    detail="the completed maintenance epoch does not match",
                )
            await self.db.commit()
            return snapshot
        if snapshot.maintenance_epoch != epoch:
            raise RuntimeMaintenanceError(
                "RUNTIME_MAINTENANCE_IDENTITY_CONFLICT",
                detail="the active maintenance epoch does not match",
            )
        row.maintenance_state = MAINTENANCE_STATE_OPEN
        row.completed_at = now or datetime.utcnow()
        row.grant_issuance_enabled = True
        return await self._commit(row)

    async def _locked_row(self) -> RuntimeMaintenanceControl:
        row = (
            await self.db.execute(
                select(RuntimeMaintenanceControl)
                .where(RuntimeMaintenanceControl.id == self.CONTROL_ID)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise RuntimeMaintenanceError(
                "RUNTIME_MAINTENANCE_CONTROL_UNINITIALIZED",
                detail="runtime_maintenance_control singleton row is missing",
            )
        return row

    async def _commit(
        self, row: RuntimeMaintenanceControl
    ) -> RuntimeMaintenanceSnapshot:
        snapshot = self._validated_snapshot(row)
        await self.db.commit()
        return snapshot

    @staticmethod
    def _validated_snapshot(row: RuntimeMaintenanceControl) -> RuntimeMaintenanceSnapshot:
        try:
            snapshot = RuntimeMaintenanceSnapshot(
                maintenance_state=str(row.maintenance_state or ""),
                maintenance_epoch=(
                    str(row.maintenance_epoch).strip()
                    if row.maintenance_epoch is not None
                    else None
                ),
                maintenance_reason=(
                    str(row.maintenance_reason).strip()
                    if row.maintenance_reason is not None
                    else None
                ),
                started_at=row.started_at,
                completed_at=row.completed_at,
                admission_generation=int(row.admission_generation),
                grant_issuance_enabled=bool(row.grant_issuance_enabled),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeMaintenanceError(
                "RUNTIME_MAINTENANCE_CONTROL_INVALID",
                detail="Runtime maintenance control fields are malformed",
            ) from exc
        if snapshot.admission_generation <= 0:
            raise RuntimeMaintenanceError(
                "RUNTIME_MAINTENANCE_CONTROL_INVALID",
                detail="admission_generation must be positive",
            )
        if snapshot.maintenance_state == MAINTENANCE_STATE_OPEN:
            valid = snapshot.grant_issuance_enabled
        elif snapshot.maintenance_state == MAINTENANCE_STATE_FROZEN:
            valid = (
                not snapshot.grant_issuance_enabled
                and snapshot.maintenance_epoch is not None
                and snapshot.maintenance_reason is not None
                and snapshot.started_at is not None
                and snapshot.completed_at is None
            )
        else:
            valid = False
        if not valid:
            raise RuntimeMaintenanceError(
                "RUNTIME_MAINTENANCE_CONTROL_INVALID",
                detail="maintenance state and grant gate are inconsistent",
            )
        return snapshot

    @staticmethod
    def _epoch(value: str) -> str:
        epoch = str(value).strip()
        if _EPOCH_RE.fullmatch(epoch) is None:
            raise ValueError("maintenance epoch is invalid")
        return epoch


__all__ = [
    "MAINTENANCE_STATE_FROZEN",
    "MAINTENANCE_STATE_OPEN",
    "RuntimeMaintenanceError",
    "RuntimeMaintenanceService",
    "RuntimeMaintenanceSnapshot",
]
