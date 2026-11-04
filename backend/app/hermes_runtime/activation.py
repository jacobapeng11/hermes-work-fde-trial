from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Protocol

import httpx

from app.hermes_runtime.protocol import HermesProtocolError
from app.hermes_runtime.schemas import RuntimeEndpoint


class ActivationHttpClient(Protocol):
    async def head(self, url: str, **kwargs: Any) -> Any: ...


class RuntimeActivator:
    def __init__(
        self,
        *,
        client: ActivationHttpClient,
        ready_timeout_seconds: float = 120.0,
        poll_interval_seconds: float = 0.25,
        max_poll_interval_seconds: float = 2.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if ready_timeout_seconds <= 0:
            raise ValueError("ready_timeout_seconds must be positive")
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must not be negative")
        if max_poll_interval_seconds < poll_interval_seconds:
            raise ValueError(
                "max_poll_interval_seconds must be at least poll_interval_seconds"
            )
        self.client = client
        self.ready_timeout_seconds = float(ready_timeout_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.max_poll_interval_seconds = float(max_poll_interval_seconds)
        self.monotonic = monotonic

    async def activate_dashboard(self, endpoint: RuntimeEndpoint) -> None:
        path = "/favicon.ico"
        url = endpoint.endpoint_base_url.rstrip("/") + path
        deadline = self.monotonic() + self.ready_timeout_seconds
        delay = self.poll_interval_seconds
        last_status_code: int | None = None

        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise HermesProtocolError(
                    "runtime dashboard activation timed out",
                    status_code=last_status_code,
                    path=path,
                )
            try:
                response = await self.client.head(
                    url,
                    headers={"Host": endpoint.endpoint_host_header},
                    timeout=min(endpoint.timeout_seconds, remaining),
                )
            except httpx.HTTPError:
                response = None

            if response is not None:
                last_status_code = int(getattr(response, "status_code", 500))
                if last_status_code < 500:
                    return

            remaining = deadline - self.monotonic()
            if remaining <= 0:
                continue
            if delay > 0:
                await asyncio.sleep(min(delay, remaining))
            delay = min(
                max(delay * 2, self.poll_interval_seconds),
                self.max_poll_interval_seconds,
            )
