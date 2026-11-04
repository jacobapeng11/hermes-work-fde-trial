from __future__ import annotations

import asyncio
from dataclasses import dataclass


class CapacityBusy(RuntimeError):
    pass


@dataclass(slots=True)
class CapacityLease:
    _gate: "CapacityGate"
    _released: bool = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._gate._release()


@dataclass(slots=True)
class QueueAdmissionLease:
    _gate: "CapacityGate"
    _released: bool = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._gate._release_queue_admission()


class CapacityGate:
    """Atomic, non-queueing Run admission gate."""

    def __init__(
        self,
        limit: int,
        *,
        busy_code: str = "APPLICATION_CAPACITY_BUSY",
    ) -> None:
        if limit <= 0:
            raise ValueError("capacity limit must be positive")
        if not busy_code.strip():
            raise ValueError("capacity busy code must not be empty")
        self.limit = int(limit)
        self.busy_code = busy_code.strip()
        self._inflight = 0
        self._lock = asyncio.Lock()
        self._changed = asyncio.Condition(self._lock)
        self._drain_reasons: set[str] = set()
        self._queue_admissions = 0
        self._rejected_total = 0

    @property
    def inflight(self) -> int:
        return self._inflight

    @property
    def draining(self) -> bool:
        return bool(self._drain_reasons)

    @property
    def queue_admissions(self) -> int:
        return self._queue_admissions

    @property
    def drain_reasons(self) -> tuple[str, ...]:
        return tuple(sorted(self._drain_reasons))

    @property
    def rejected_total(self) -> int:
        return self._rejected_total

    async def restore(self, unfinished_count: int) -> None:
        if unfinished_count < 0 or unfinished_count > self.limit:
            raise ValueError("unfinished Run count exceeds configured capacity")
        async with self._changed:
            self._inflight = unfinished_count
            self._changed.notify_all()

    async def release_recovered(self) -> None:
        """Release capacity restored without an in-memory lease."""

        await self._release()

    async def try_acquire(self) -> CapacityLease:
        async with self._changed:
            if self.draining:
                self._rejected_total += 1
                raise CapacityBusy("RUNTIME_DRAINING")
            if self._inflight >= self.limit:
                self._rejected_total += 1
                raise CapacityBusy(self.busy_code)
            self._inflight += 1
            self._changed.notify_all()
        return CapacityLease(self)

    async def acquire_queue_admission(self) -> QueueAdmissionLease:
        """Fence durable queue insertion against an administrative drain."""

        async with self._changed:
            if self.draining:
                self._rejected_total += 1
                raise CapacityBusy("RUNTIME_DRAINING")
            self._queue_admissions += 1
            self._changed.notify_all()
        return QueueAdmissionLease(self)

    async def begin_drain(self, reason: str = "default") -> None:
        async with self._changed:
            self._drain_reasons.add(reason)
            self._changed.notify_all()
            await self._changed.wait_for(
                lambda: self._queue_admissions == 0
            )

    async def cancel_drain(self, reason: str = "default") -> None:
        async with self._changed:
            self._drain_reasons.discard(reason)
            self._changed.notify_all()

    async def wait_until_empty(self, *, timeout_seconds: float) -> bool:
        async def _wait() -> None:
            async with self._changed:
                await self._changed.wait_for(lambda: self._inflight == 0)

        try:
            await asyncio.wait_for(_wait(), timeout=max(0.001, timeout_seconds))
        except asyncio.TimeoutError:
            return False
        return True

    async def _release(self) -> None:
        async with self._changed:
            if self._inflight <= 0:
                raise RuntimeError("capacity lease released without an active Run")
            self._inflight -= 1
            self._changed.notify_all()

    async def _release_queue_admission(self) -> None:
        async with self._changed:
            if self._queue_admissions <= 0:
                raise RuntimeError(
                    "queue admission released without an active submit"
                )
            self._queue_admissions -= 1
            self._changed.notify_all()
