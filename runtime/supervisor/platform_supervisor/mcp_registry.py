from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import logging
from typing import Any, Awaitable, Callable, Protocol

from platform_supervisor.asset_contracts import (
    RuntimeMcpRegistryApplyRequest,
    RuntimeMcpRegistryApplyResponse,
    RuntimeMcpRegistryServer,
    RuntimeMcpRegistryServerEvidence,
    validate_mcp_connection_target,
)
from platform_supervisor.mcp_registry_state import (
    McpRegistryStateError,
    McpRegistryStateStore,
)
from platform_supervisor.toolset_policy import normalize_platform_cli_toolsets


class McpRegistryError(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


logger = logging.getLogger(__name__)


class McpRegistryExecutionGate:
    """Pause only not-yet-dispatched Runs during a process-global reload."""

    def __init__(self, *, state: McpRegistryStateStore, resolver) -> None:
        self.state = state
        self.resolver = resolver

    async def __call__(self, identity):
        if await self.state.is_reloading():
            return None
        return await self.resolver(identity)


class McpRegistryDashboard(Protocol):
    async def read_profile_config(self, profile_name: str) -> dict[str, Any]: ...

    async def write_profile_config(
        self,
        profile_name: str,
        document: dict[str, Any],
    ) -> None: ...

    async def reload_mcp_registry_and_list(self) -> list[dict[str, Any]]: ...

    async def list_mcp_registry_servers(self) -> list[dict[str, Any]]: ...


class RuntimeMcpRegistryController:
    """Apply the one process-global MCP Registry without granting default tools."""

    def __init__(
        self,
        *,
        runtime_id: str,
        binding_id: str,
        state: McpRegistryStateStore,
        dashboard: McpRegistryDashboard,
        active_run_ids,
        config_lock: asyncio.Lock | None = None,
        discovery_attempts: int = 60,
        discovery_interval_seconds: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.runtime_id = runtime_id
        self.binding_id = binding_id
        self.state = state
        self.dashboard = dashboard
        self.active_run_ids = active_run_ids
        self._config_lock = config_lock or asyncio.Lock()
        self._apply_lock = asyncio.Lock()
        self._terminal_sink: Callable[[], Awaitable[None]] | None = None
        self._discovery_attempts = max(1, discovery_attempts)
        self._discovery_interval_seconds = discovery_interval_seconds
        self._sleep = sleep

    def bind_terminal_sink(
        self,
        sink: Callable[[], Awaitable[None]] | None,
    ) -> None:
        self._terminal_sink = sink

    async def apply(
        self,
        request: RuntimeMcpRegistryApplyRequest,
    ) -> RuntimeMcpRegistryApplyResponse:
        self._validate_target(request)
        for server in request.servers:
            try:
                validate_mcp_connection_target(
                    server.transport,
                    server.connection_config,
                )
            except ValueError as exc:
                return RuntimeMcpRegistryApplyResponse(
                    operation_id=request.operation_id,
                    desired_revision=request.desired_revision,
                    applied_revision=request.expected_applied_revision,
                    status="failed",
                    registry_digest=request.registry_digest,
                    error_code="MCP_CONFIG_INVALID",
                    error_message=str(exc),
                )
        async with self._apply_lock:
            return await self._apply_locked(request)

    async def _apply_locked(
        self,
        request: RuntimeMcpRegistryApplyRequest,
    ) -> RuntimeMcpRegistryApplyResponse:
        affected_run_ids = list(self.active_run_ids())
        try:
            observed = await self.state.begin(
                request,
                affected_run_ids=affected_run_ids,
            )
        except McpRegistryStateError as exc:
            raise McpRegistryError(str(exc)) from exc
        if observed.status == "ready":
            document = await self.dashboard.read_profile_config("default")
            if self._project_default_config(document, request) == document:
                return observed.response()
        try:
            async with self._config_lock:
                document = await self.dashboard.read_profile_config("default")
                projected = self._project_default_config(document, request)
                await self.dashboard.write_profile_config("default", projected)
                readback = await self.dashboard.read_profile_config("default")
                if readback != projected:
                    raise McpRegistryError("MCP_REGISTRY_CONFIG_VERIFY_FAILED")
                observed_servers = (
                    await self.dashboard.reload_mcp_registry_and_list()
                )
                evidence = await self._wait_for_evidence(
                    request,
                    observed_servers,
                )
            return await self._finish(request, status="ready", servers=evidence)
        except McpRegistryError as exc:
            return await self._finish(
                request,
                status="failed",
                servers=[],
                error_code=exc.code,
                error_message=str(exc),
            )
        except Exception as exc:
            return await self._finish(
                request,
                status="failed",
                servers=[],
                error_code="MCP_REGISTRY_APPLY_FAILED",
                error_message=type(exc).__name__,
            )

    async def _wait_for_evidence(
        self,
        request: RuntimeMcpRegistryApplyRequest,
        observed_servers: list[dict[str, Any]],
    ) -> list[RuntimeMcpRegistryServerEvidence]:
        try:
            return self._evidence(request, observed_servers)
        except McpRegistryError as exc:
            if not self._is_pending_discovery(exc):
                raise
            last_error = exc

        for _attempt in range(self._discovery_attempts):
            await self._sleep(self._discovery_interval_seconds)
            observed_servers = await self.dashboard.list_mcp_registry_servers()
            try:
                return self._evidence(request, observed_servers)
            except McpRegistryError as exc:
                if not self._is_pending_discovery(exc):
                    raise
                last_error = exc
        raise last_error

    @staticmethod
    def _is_pending_discovery(exc: McpRegistryError) -> bool:
        return exc.code in {
            "MCP_SERVER_NOT_INITIALIZED",
            "MCP_TOOLS_LIST_MISSING",
        }

    async def status(self) -> RuntimeMcpRegistryApplyResponse:
        state = await self.state.get()
        if state is None:
            raise McpRegistryError("MCP_REGISTRY_NOT_FOUND")
        return state.response()

    async def _finish(
        self,
        request: RuntimeMcpRegistryApplyRequest,
        *,
        status: str,
        servers: list[RuntimeMcpRegistryServerEvidence],
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> RuntimeMcpRegistryApplyResponse:
        response = (
            await self.state.finish(
                request,
                status=status,
                servers=servers,
                error_code=error_code,
                error_message=error_message,
            )
        ).response()
        if self._terminal_sink is not None:
            try:
                await self._terminal_sink()
            except Exception as exc:
                logger.warning(
                    "mcp_registry_terminal_wake_failed",
                    extra={"error_type": type(exc).__name__},
                )
        return response

    def _validate_target(self, request: RuntimeMcpRegistryApplyRequest) -> None:
        if request.runtime_id != self.runtime_id:
            raise McpRegistryError("MCP_REGISTRY_RUNTIME_MISMATCH")
        if str(request.binding_id) != self.binding_id:
            raise McpRegistryError("MCP_REGISTRY_BINDING_MISMATCH")
        slugs = [server.server_slug for server in request.servers]
        if len(slugs) != len(set(slugs)):
            raise McpRegistryError("MCP_REGISTRY_SLUG_COLLISION")

    @staticmethod
    def _project_default_config(
        current: dict[str, Any],
        request: RuntimeMcpRegistryApplyRequest,
    ) -> dict[str, Any]:
        document = deepcopy(current)
        document["mcp_servers"] = {
            server.server_slug: RuntimeMcpRegistryController._server_config(server)
            for server in request.servers
        }
        toolsets = document.get("platform_toolsets")
        if not isinstance(toolsets, dict):
            toolsets = {}
            document["platform_toolsets"] = toolsets
        cli = toolsets.get("cli")
        valid_mcp_toolsets = {
            f"mcp-{server.server_slug}" for server in request.servers
        }
        toolsets["cli"] = normalize_platform_cli_toolsets(
            cli if isinstance(cli, list) else (),
            valid_mcp_toolsets=valid_mcp_toolsets,
        )
        platform = document.get("platform")
        if not isinstance(platform, dict):
            platform = {}
            document["platform"] = platform
        platform["mcp_registry_projection"] = {
            "desired_revision": request.desired_revision,
            "registry_digest": request.registry_digest,
        }
        return document

    @staticmethod
    def _server_config(server: RuntimeMcpRegistryServer) -> dict[str, Any]:
        config = deepcopy(server.connection_config)
        credential_target = "env" if server.transport == "stdio" else "headers"
        existing = config.get(credential_target)
        if existing is not None and not isinstance(existing, dict):
            raise McpRegistryError("MCP_CREDENTIAL_CONFIG_INVALID")
        if server.credential_refs:
            config[credential_target] = {
                **deepcopy(existing or {}),
                **deepcopy(server.credential_refs),
            }
        config["transport"] = server.transport
        config["enabled"] = True
        return config

    @classmethod
    def _evidence(
        cls,
        request: RuntimeMcpRegistryApplyRequest,
        observed: list[dict[str, Any]],
    ) -> list[RuntimeMcpRegistryServerEvidence]:
        by_slug: dict[str, dict[str, Any]] = {}
        for item in observed:
            slug = next(
                (
                    str(item.get(key) or "").strip()
                    for key in ("slug", "name", "id", "server_name", "serverName")
                    if str(item.get(key) or "").strip()
                ),
                "",
            )
            if slug:
                by_slug[slug] = item
        evidence: list[RuntimeMcpRegistryServerEvidence] = []
        final_tool_names: set[str] = set()
        for server in request.servers:
            item = by_slug.get(server.server_slug)
            if item is None:
                raise McpRegistryError("MCP_SERVER_NOT_INITIALIZED")
            if item.get("toolset_available") is False:
                raise McpRegistryError("MCP_SERVER_NOT_INITIALIZED")
            raw_tools = item.get("tools")
            if not isinstance(raw_tools, list):
                raise McpRegistryError("MCP_TOOLS_LIST_MISSING")
            tool_names = []
            for tool in raw_tools:
                name = (
                    str(tool.get("name") or "").strip()
                    if isinstance(tool, dict)
                    else str(tool).strip()
                )
                if not name:
                    raise McpRegistryError("MCP_TOOL_SCHEMA_INVALID")
                if name in final_tool_names:
                    raise McpRegistryError("MCP_TOOL_NAME_COLLISION")
                final_tool_names.add(name)
                tool_names.append(name)
            schema_digest = cls._digest(raw_tools)
            evidence.append(
                RuntimeMcpRegistryServerEvidence(
                    server_slug=server.server_slug,
                    status="ready",
                    tools_schema_digest=schema_digest,
                    tool_names=tool_names,
                )
            )
        return evidence

    @staticmethod
    def _digest(value: object) -> str:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


__all__ = [
    "McpRegistryDashboard",
    "McpRegistryError",
    "McpRegistryExecutionGate",
    "RuntimeMcpRegistryController",
]
