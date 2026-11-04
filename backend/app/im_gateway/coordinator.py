from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from app.config import settings


logger = logging.getLogger(__name__)


class ImGatewayCoordinator:
    """Lifespan facade for the Backend-owned IM domain.

    Connection actors and queue workers are installed as factories to keep
    provider dependencies out of API import paths.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._config_event = asyncio.Event()
        self._reconcile: Callable[[], Awaitable[None]] | None = None
        self._close: Callable[[], Awaitable[None]] | None = None
        self._notify: (
            Callable[[int, int], Awaitable[None]] | None
        ) = None
        self._last_success_at: datetime | None = None
        self._last_error_code: str | None = None
        self._consecutive_failures = 0

    def configure(
        self,
        *,
        reconcile: Callable[[], Awaitable[None]],
        close: Callable[[], Awaitable[None]] | None = None,
        notify: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> None:
        self._reconcile = reconcile
        self._close = close
        self._notify = notify

    def start(self) -> None:
        if not settings.IM_GATEWAY_ENABLED:
            return
        if self._task is None or self._task.done():
            self._stop_event = asyncio.Event()
            self._last_error_code = None
            self._consecutive_failures = 0
            self._task = asyncio.create_task(
                self._run_forever(), name="im-gateway-coordinator"
            )

    async def stop(self) -> None:
        self._stop_event.set()
        self._config_event.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (
                asyncio.CancelledError,
                asyncio.TimeoutError,
                TimeoutError,
            ):
                pass
        if self._close is not None:
            await self._close()

    async def notify_config_changed(
        self, channel_id: int, config_generation: int
    ) -> None:
        if self._notify is not None:
            await self._notify(channel_id, config_generation)
        self._config_event.set()

    def request_reconcile(self) -> None:
        """Wake reconciliation after a bulk state transition.

        Bulk deletion can remove the channel row before a per-channel
        generation notification is possible.  The actor still needs to be
        disconnected immediately instead of waiting for the recovery scan.
        """

        self._config_event.set()

    async def _run_forever(self) -> None:
        if self._reconcile is None:
            from app.im_gateway.runtime import gateway_runtime

            self.configure(
                reconcile=gateway_runtime.reconcile,
                close=gateway_runtime.close,
                notify=gateway_runtime.notify_config_changed,
            )
        while not self._stop_event.is_set():
            try:
                if self._reconcile is not None:
                    await self._reconcile()
                self._last_success_at = datetime.now(timezone.utc)
                self._last_error_code = None
                self._consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error_code = type(exc).__name__
                self._consecutive_failures += 1
                logger.exception("IM Gateway reconciliation failed")
            self._config_event.clear()
            try:
                await asyncio.wait_for(
                    self._config_event.wait(),
                    timeout=settings.IM_GATEWAY_RECONCILE_INTERVAL_SECONDS,
                )
            except (asyncio.TimeoutError, TimeoutError):
                pass

    @property
    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done()

    def health(self) -> dict[str, Any]:
        if not settings.IM_GATEWAY_ENABLED:
            status = "disabled"
        elif not self.is_alive or self._consecutive_failures:
            status = "degraded"
        elif self._last_success_at is None:
            status = "starting"
        else:
            status = "ok"
        return {
            "status": status,
            "enabled": settings.IM_GATEWAY_ENABLED,
            "coordinator_alive": self.is_alive,
            "last_success_at": (
                self._last_success_at.isoformat()
                if self._last_success_at is not None
                else None
            ),
            "consecutive_failures": self._consecutive_failures,
            "last_error_code": self._last_error_code,
        }


im_gateway = ImGatewayCoordinator()
