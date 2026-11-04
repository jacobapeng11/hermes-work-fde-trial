from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy import update

from app.models import HermesRuntimeBinding
from app.services.runtime_maintenance import (
    RuntimeMaintenanceError,
    RuntimeMaintenanceService,
    RuntimeMaintenanceSnapshot,
)

RuntimeReconcileTarget = Literal[
    "starting",
    "restart_requested",
    "force_stop_requested",
]


class AdmissionSession(Protocol):
    async def execute(self, statement: object): ...

    async def commit(self) -> None: ...


class MaintenanceAdmissionControl(Protocol):
    async def require_admission_open(
        self,
        *,
        lock: bool = False,
    ) -> RuntimeMaintenanceSnapshot: ...


@dataclass(frozen=True)
class RuntimeAdmissionDecision:
    allowed: bool
    retryable: bool
    code: str


class RuntimeAdmissionError(RuntimeError):
    def __init__(self, decision: RuntimeAdmissionDecision) -> None:
        super().__init__(decision.code)
        self.decision = decision

    @classmethod
    def reconcile_in_progress(cls) -> "RuntimeAdmissionError":
        return cls(
            RuntimeAdmissionDecision(
                allowed=False,
                retryable=True,
                code="RUNTIME_RECONCILE_IN_PROGRESS",
            )
        )


class RuntimeAdmissionService:
    _DECISIONS = {
        "ready": RuntimeAdmissionDecision(True, False, "RUNTIME_READY"),
        "force_stopped": RuntimeAdmissionDecision(
            False, False, "RUNTIME_FORCE_STOPPED"
        ),
        "force_stop_requested": RuntimeAdmissionDecision(
            False, False, "RUNTIME_FORCE_STOPPING"
        ),
        "force_stop_failed": RuntimeAdmissionDecision(
            False, False, "RUNTIME_FORCE_STOP_FAILED"
        ),
        "restart_requested": RuntimeAdmissionDecision(
            False, True, "RUNTIME_RESTARTING"
        ),
        "starting": RuntimeAdmissionDecision(False, True, "RUNTIME_STARTING"),
        "failed": RuntimeAdmissionDecision(False, True, "RUNTIME_FAILED"),
        "scale_to_zero_failed": RuntimeAdmissionDecision(
            False, True, "RUNTIME_INFRASTRUCTURE_FAILED"
        ),
    }
    _TARGET_STATUSES = frozenset(
        {"starting", "restart_requested", "force_stop_requested"}
    )
    _RECONCILE_SOURCE_STATUSES = ("ready", "failed")

    def __init__(
        self,
        session: AdmissionSession,
        *,
        maintenance_control: MaintenanceAdmissionControl | None = None,
    ) -> None:
        self.session = session
        self.maintenance_control = maintenance_control or RuntimeMaintenanceService(
            session  # type: ignore[arg-type]
        )

    @classmethod
    def decide(cls, binding_status: str) -> RuntimeAdmissionDecision:
        return cls._DECISIONS.get(
            str(binding_status or ""),
            RuntimeAdmissionDecision(False, True, "RUNTIME_NOT_READY"),
        )

    @classmethod
    def require_ready(cls, binding_status: str) -> None:
        decision = cls.decide(binding_status)
        if not decision.allowed:
            raise RuntimeAdmissionError(decision)

    async def require_new_work_allowed(
        self,
        *,
        lock: bool = False,
    ) -> RuntimeMaintenanceSnapshot:
        try:
            return await self.maintenance_control.require_admission_open(lock=lock)
        except RuntimeMaintenanceError as exc:
            raise RuntimeAdmissionError(
                RuntimeAdmissionDecision(
                    allowed=False,
                    retryable=exc.retryable,
                    code=exc.code,
                )
            ) from exc

    async def begin_reconcile(
        self,
        runtime_workspace_id: int,
        target_status: RuntimeReconcileTarget,
        *,
        target_image: str | None = None,
    ) -> bool:
        if runtime_workspace_id <= 0:
            raise ValueError("runtime_workspace_id must be positive")
        if target_status not in self._TARGET_STATUSES:
            raise ValueError("unsupported Runtime reconcile target status")
        await self.require_new_work_allowed(lock=True)
        values: dict[str, object] = {"status": target_status}
        if target_image is not None:
            image = target_image.strip()
            if not image:
                raise ValueError("target image must not be empty")
            values["image"] = image
        result = await self.session.execute(
            update(HermesRuntimeBinding)
            .where(
                HermesRuntimeBinding.runtime_workspace_id
                == runtime_workspace_id,
                HermesRuntimeBinding.status.in_(self._RECONCILE_SOURCE_STATUSES),
            )
            .values(**values)
        )
        await self.session.commit()
        return int(getattr(result, "rowcount", 0) or 0) == 1

    async def mark_failed(self, runtime_workspace_id: int, message: str) -> bool:
        cleaned = " ".join(str(message).split())[:1000]
        result = await self.session.execute(
            update(HermesRuntimeBinding)
            .where(
                HermesRuntimeBinding.runtime_workspace_id
                == runtime_workspace_id,
                HermesRuntimeBinding.status.in_(tuple(self._TARGET_STATUSES)),
            )
            .values(
                status="failed",
                pending_reload=True,
                last_error=cleaned or "runtime topology reconcile failed",
            )
        )
        await self.session.commit()
        return int(getattr(result, "rowcount", 0) or 0) == 1
