from __future__ import annotations

import asyncio
from contextlib import contextmanager
from collections.abc import Iterator

import uvicorn

from platform_supervisor.model_relay.app import create_model_relay_app
from platform_supervisor.model_relay.relay import ModelRelay, ModelRouteRegistry
from platform_supervisor.settings import SupervisorSettings


class _EmbeddedUvicornServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The outer Supervisor server is PID 1 and owns process signals.
        yield


class ModelRelayServer:
    def __init__(
        self,
        settings: SupervisorSettings,
        relay: ModelRelay,
        registry: ModelRouteRegistry,
    ) -> None:
        self.settings = settings
        self.relay = relay
        self.registry = registry
        self.app = create_model_relay_app(relay, registry)
        self.config = uvicorn.Config(
            self.app,
            host=settings.MODEL_RELAY_HOST,
            port=settings.MODEL_RELAY_PORT,
            access_log=False,
            log_level="info",
            lifespan="off",
        )
        self.server = _EmbeddedUvicornServer(self.config)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self.server.serve(),
            name="model-relay-server",
        )
        while not self.server.started:
            if self._task.done():
                await self._task
                raise RuntimeError("Model Relay server exited during startup")
            await asyncio.sleep(0.01)
        self.relay.start_transport_warmup()

    async def stop(self) -> None:
        if self._task is not None:
            self.server.should_exit = True
            await self._task
            self._task = None
        await self.relay.aclose()
