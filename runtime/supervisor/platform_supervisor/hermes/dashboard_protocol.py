from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import os
import re
import shlex
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, AsyncContextManager, BinaryIO, Protocol
from urllib.parse import quote, urlencode, urlparse
from uuid import uuid4

import httpx
import websockets

from platform_supervisor.contracts import RuntimeTurnAttachment
from platform_supervisor.hermes.dashboard_hot_channel import (
    ActiveRunControlPeerLease,
    ActiveRunControlPeerRegistry,
    DashboardHotChannelIdentity,
    DashboardHotChannelManager,
    DashboardHotCapacityUnavailable,
    DashboardHotRunLease,
    DashboardInterruptSettleResult,
    DashboardPeerDisconnected,
    DashboardPreparedPeer,
    DashboardPromptSubmitTimeout,
    DashboardResumeRequired,
    DashboardRpcPeer,
    DashboardRpcPeerSocket,
    dashboard_session_workspace_path,
    shared_active_run_control_peers,
    shared_dashboard_hot_channel_manager,
)
from platform_supervisor.hermes.protocol import HermesProtocolError
from platform_supervisor.hermes.public_tool_payload import build_public_tool_payload
from platform_supervisor.hermes.capability_errors import (
    capability_error_from_tool_payload,
)
from platform_supervisor.hermes.skill_manage_observation import (
    extract_skill_manage_completion,
    public_skill_manage_args,
    public_skill_manage_result,
)
from platform_supervisor.hermes.schemas import RuntimeEndpoint
from platform_supervisor.hermes.events import PlatformStreamEvent

logger = logging.getLogger(__name__)
timing_logger = logging.getLogger("uvicorn.error.runtime_timing")
_INTERRUPT_WAITING_FOR_MODEL_PREFIX = "Operation interrupted: waiting for model response ("
_CONTEXT_COMPRESSION_EXHAUSTED_PUBLIC_MESSAGE = "当前上下文压缩已经达到10次，你可以选择是否要继续"
_CONTEXT_COMPRESSION_EXHAUSTED_RE = re.compile(
    r"^(?:Error: )?Context length exceeded: max compression attempts \(10\) reached\.$"
)
_PLATFORM_MODEL_PROVIDER = "custom:platform"
_LIVE_SESSION_RELEASE_AUTHORITY = object()
_ROUTE_PREPARE_PROOF_AUTHORITY = object()
_DASHBOARD_MESSAGE_PAGE_SIZE = 500
_DASHBOARD_SESSION_LIST_PAGE_SIZE = 100
_HERMES_CHILD_EXECUTABLE = "/opt/hermes/.venv/bin/platform-hermes-child-exec"
_HERMES_PYTHON = "/opt/hermes/.venv/bin/python"
_SESSION_METADATA_HELPER = "/opt/hermes-platform/bin/hermes-session-metadata-helper.py"
_DASHBOARD_TIMING_PAIRS = (
    ("dashboard_stream_begin", "hot_channel_acquired"),
    ("runtime_submitted", "hot_channel_acquired"),
    ("dashboard_stream_begin", "dashboard_connection_acquired"),
    ("runtime_submitted", "dashboard_connection_acquired"),
    ("dashboard_connection_acquired", "workspace_ready"),
    ("workspace_ready", "session_resumed"),
    ("session_resumed", "model_ready"),
    ("model_ready", "cwd_ready"),
    ("cwd_ready", "prompt_submit_begin"),
    ("dashboard_connection_acquired", "prompt_submit_begin"),
    ("prompt_submit_begin", "prompt_ack"),
)


class _DashboardSessionLineageChanged(RuntimeError):
    pass


def _safe_timing_snapshot(timing: Any) -> dict[str, object]:
    snapshot = getattr(timing, "safe_snapshot", None)
    if not callable(snapshot):
        return {}
    try:
        result = snapshot(duration_pairs=_DASHBOARD_TIMING_PAIRS)
    except TypeError:
        try:
            result = snapshot()
        except Exception:
            return {}
    except Exception:
        return {}
    return result if isinstance(result, dict) else {}


def _log_dashboard_timing(
    timing: Any,
    *,
    stage: str,
    status: str,
    error_type: str | None = None,
) -> None:
    payload: dict[str, object] = {
        "component": "dashboard_protocol",
        "stage": stage,
        "status": status,
        **_safe_timing_snapshot(timing),
    }
    if error_type is not None:
        payload["error_type"] = error_type
    try:
        timing_logger.info("agent ttft timing=%s", payload)
    except Exception:
        # Observability must never replace the protocol result or exception.
        pass


def _is_waiting_for_model_interrupt(text: object) -> bool:
    return isinstance(text, str) and text.strip().startswith(
        _INTERRUPT_WAITING_FOR_MODEL_PREFIX
    )


def _public_terminal_payload(payload: dict[str, Any]) -> dict[str, Any]:
    compression_exhausted = any(
        isinstance(value, str)
        and _CONTEXT_COMPRESSION_EXHAUSTED_RE.fullmatch(value.strip())
        for value in (payload.get("text"), payload.get("error"), payload.get("rendered"))
    )
    if not compression_exhausted:
        return payload

    public_payload = dict(payload)
    for field_name in ("text", "error", "rendered"):
        value = public_payload.get(field_name)
        if isinstance(value, str) and _CONTEXT_COMPRESSION_EXHAUSTED_RE.fullmatch(
            value.strip()
        ):
            public_payload[field_name] = _CONTEXT_COMPRESSION_EXHAUSTED_PUBLIC_MESSAGE
    return public_payload


def _public_chat_error_payload(message: object) -> dict[str, object]:
    text = str(message)
    if "model relay route has expired" in text:
        return {
            "code": "MODEL_ROUTE_EXPIRED",
            "message": "任务执行时间过长，模型调用授权已过期，请重新发送任务",
        }
    return {"message": text}


def _hot_channel_expected_model(
    identity: DashboardHotChannelIdentity,
) -> str | None:
    return str(identity.gateway_model_id or "").strip() or None


class DashboardWebSocket(Protocol):
    async def send(self, payload: str) -> None: ...

    async def recv(self) -> str | bytes: ...


DashboardConnector = Callable[[RuntimeEndpoint], AsyncContextManager[DashboardWebSocket]]


@dataclass(frozen=True)
class ManagedRuntimeArtifact:
    relative_path: str
    mime_type: str | None
    size_bytes: int
    mtime: float | None = None


@dataclass(frozen=True, slots=True)
class _DashboardLiveSessionRelease:
    runtime_session_id: str
    _authority: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class DashboardSessionRouteProof:
    """Non-serializable evidence for one completed Supervisor prepare path."""

    runtime_session_id: str
    hermes_profile_name: str
    expected_model: str
    expected_provider: str
    _authority: object = field(repr=False, compare=False)


def _managed_entry_mtime(entry: dict[str, Any]) -> float | None:
    raw_mtime = entry.get("mtime")
    if raw_mtime is None:
        return None
    try:
        mtime = float(raw_mtime)
    except (TypeError, ValueError):
        return None
    return mtime if math.isfinite(mtime) else None


@dataclass(frozen=True)
class _ChatConnection:
    websocket: DashboardWebSocket
    hot_lease: DashboardHotRunLease | None = None
    control_lease: ActiveRunControlPeerLease | None = None
    live_session_id: str | None = None
    workspace: str | None = None
    model: str | None = None
    provider: str | None = None


class ProgressMarkerBuffer:
    """Remove explicit Agent progress markers from normal assistant output."""

    OPEN = "<platform_progress>"
    CLOSE = "</platform_progress>"

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, text: str) -> tuple[str, list[str]]:
        self._buffer += text
        visible: list[str] = []
        labels: list[str] = []
        while self._buffer:
            start = self._buffer.find(self.OPEN)
            if start < 0:
                keep = self._prefix_suffix_length(self._buffer, self.OPEN)
                if keep:
                    visible.append(self._buffer[:-keep])
                    self._buffer = self._buffer[-keep:]
                else:
                    visible.append(self._buffer)
                    self._buffer = ""
                break
            if start:
                visible.append(self._buffer[:start])
                self._buffer = self._buffer[start:]
            end = self._buffer.find(self.CLOSE, len(self.OPEN))
            if end < 0:
                break
            label = self._sanitize_label(self._buffer[len(self.OPEN):end])
            if label is not None:
                labels.append(label)
            self._buffer = self._buffer[end + len(self.CLOSE):]
        return "".join(visible), labels

    def strip_complete(self, text: str) -> tuple[str, list[str]]:
        labels: list[str] = []

        def replace(match: re.Match[str]) -> str:
            label = self._sanitize_label(match.group(1))
            if label is not None:
                labels.append(label)
            return ""

        clean = re.sub(
            re.escape(self.OPEN) + r"(.*?)" + re.escape(self.CLOSE),
            replace,
            text,
            flags=re.DOTALL,
        )
        return clean, labels

    @staticmethod
    def _sanitize_label(value: str) -> str | None:
        label = " ".join(value.split())
        return label if 0 < len(label) <= 80 else None

    @staticmethod
    def _prefix_suffix_length(value: str, marker: str) -> int:
        upper = min(len(value), len(marker) - 1)
        for size in range(upper, 0, -1):
            if value.endswith(marker[:size]):
                return size
        return 0


@dataclass
class _DashboardHttpSession:
    client: httpx.AsyncClient
    base_url: str
    headers: dict[str, str]


class DashboardHttpSessionPool:
    """Reuse Dashboard session-token auth and HTTP keep-alive connections.

    A WebSocket still needs a fresh one-use ticket for each connection, but a
    separate password login is neither needed nor valid for Dashboard's
    loopback mode.  The same process-lifetime token protects REST and ticket
    requests without adding a login round trip to the first turn.
    """

    def __init__(
        self,
    ) -> None:
        self._sessions: dict[tuple[str, str, str], _DashboardHttpSession] = {}
        self._guard = asyncio.Lock()

    @staticmethod
    def _key(endpoint: RuntimeEndpoint) -> tuple[str, str, str]:
        route_host = endpoint.endpoint_host_header.strip()
        if not route_host:
            raise HermesProtocolError("runtime dashboard route host is unavailable")
        return (
            endpoint.endpoint_base_url.rstrip("/"),
            route_host,
            hashlib.sha256(endpoint.api_key.encode()).hexdigest(),
        )

    async def acquire(self, endpoint: RuntimeEndpoint) -> _DashboardHttpSession:
        if not endpoint.api_key:
            raise HermesProtocolError("runtime dashboard credentials are unavailable")
        base = urlparse(endpoint.endpoint_base_url)
        if not base.hostname:
            raise HermesProtocolError("runtime dashboard URL is invalid")
        key = self._key(endpoint)
        async with self._guard:
            session = self._sessions.get(key)
            if session is None or session.client.is_closed:
                session = _DashboardHttpSession(
                    client=httpx.AsyncClient(
                        timeout=endpoint.timeout_seconds,
                        trust_env=False,
                        follow_redirects=False,
                        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
                    ),
                    base_url=key[0],
                    headers={
                        "Host": key[1],
                        "X-Hermes-Session-Token": endpoint.api_key,
                    },
                )
                self._sessions[key] = session
        return session

    async def request(
        self,
        endpoint: RuntimeEndpoint,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> Any:
        if not path.startswith("/"):
            raise ValueError("dashboard request path must be absolute")
        last_error: BaseException | None = None
        for attempt in range(2):
            session = await self.acquire(endpoint)
            kwargs: dict[str, Any] = {
                "headers": session.headers,
                "timeout": endpoint.timeout_seconds,
            }
            if json is not None:
                kwargs["json"] = json
            try:
                response = await session.client.request(
                    method,
                    f"{session.base_url}{path}",
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                last_error = exc
                await self.invalidate(endpoint)
                if attempt == 0:
                    continue
                break
            if response.status_code not in {401, 403} or attempt == 1:
                return response
            await self.invalidate(endpoint)
        raise HermesProtocolError(
            f"runtime dashboard request failed: {last_error or 'authentication failed'}",
            path=path,
        )

    async def invalidate(self, endpoint: RuntimeEndpoint) -> None:
        key = self._key(endpoint)
        async with self._guard:
            session = self._sessions.pop(key, None)
        if session is not None:
            await session.client.aclose()

    async def aclose(self) -> None:
        async with self._guard:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        await asyncio.gather(*(session.client.aclose() for session in sessions), return_exceptions=True)


_dashboard_http_sessions = DashboardHttpSessionPool()


async def close_dashboard_http_sessions() -> None:
    await _dashboard_http_sessions.aclose()


@asynccontextmanager
async def authenticated_dashboard_client(endpoint: RuntimeEndpoint):
    session = await _dashboard_http_sessions.acquire(endpoint)
    yield session.client, session.base_url, session.headers


@asynccontextmanager
async def connect_dashboard(endpoint: RuntimeEndpoint) -> AsyncIterator[DashboardWebSocket]:
    """Authenticate to Dashboard and open its public JSON-RPC WebSocket."""
    base = urlparse(endpoint.endpoint_base_url)
    if not base.hostname:
        raise HermesProtocolError("runtime dashboard URL is invalid")
    route_host = endpoint.endpoint_host_header.strip()
    if not route_host:
        raise HermesProtocolError("runtime dashboard route host is unavailable")
    if not endpoint.api_key:
        raise HermesProtocolError("runtime dashboard credentials are unavailable")

    try:
        ws_scheme = "wss" if base.scheme == "https" else "ws"
        # Hermes loopback mode uses the same process-lifetime session token
        # directly on WebSocket upgrades.  One-use tickets belong exclusively
        # to its remote OAuth/Cookie gate and return 401 on localhost.
        ws_uri = (
            f"{ws_scheme}://{route_host}/api/ws?"
            f"token={quote(endpoint.api_key, safe='')}"
        )
        port = base.port or (443 if base.scheme == "https" else 80)
        async with websockets.connect(
            ws_uri,
            host=base.hostname,
            port=port,
            proxy=None,
            open_timeout=endpoint.timeout_seconds,
            ping_interval=20,
            ping_timeout=60,
            max_size=4 * 1024 * 1024,
        ) as websocket:
            yield websocket
    except HermesProtocolError:
        raise
    except Exception as exc:
        raise HermesProtocolError(f"runtime dashboard connection failed: {exc}") from exc


class HermesDashboardProtocolAdapter:
    """Use Dashboard management REST and JSON-RPC for the Web chat lifecycle."""

    def __init__(
        self,
        *,
        connector: DashboardConnector | None = connect_dashboard,
        reconnect_attempts: int = 8,
        reconnect_backoff_seconds: float = 0.5,
        prewarm_attempts: int = 3,
        prewarm_backoff_seconds: float = 0.5,
        resume_poll_seconds: float = 15.0,
        session_workspace_root: str | None = None,
        hot_channels: DashboardHotChannelManager | None = None,
        control_peers: ActiveRunControlPeerRegistry | None = None,
        allow_active_mcp_reload: bool = False,
    ) -> None:
        self.connector = connector
        self.reconnect_attempts = max(0, reconnect_attempts)
        self.reconnect_backoff_seconds = max(0.0, reconnect_backoff_seconds)
        self.prewarm_attempts = max(1, prewarm_attempts)
        self.prewarm_backoff_seconds = max(0.0, prewarm_backoff_seconds)
        self.resume_poll_seconds = max(0.1, resume_poll_seconds)
        self.session_workspace_root = session_workspace_root.rstrip("/") if session_workspace_root else None
        self.hot_channels = hot_channels or shared_dashboard_hot_channel_manager()
        self.control_peers = control_peers or shared_active_run_control_peers()
        self.allow_active_mcp_reload = allow_active_mcp_reload

    async def health(self, endpoint: RuntimeEndpoint) -> dict[str, Any]:
        status = await self._dashboard_json_request(
            endpoint,
            "GET",
            "/api/status",
        )
        sessions = await self._dashboard_json_request(
            endpoint,
            "GET",
            self._query_path("/api/sessions", [("limit", 1)]),
        )
        version = str(status.get("version") or "").strip()
        # The Dashboard is intentionally loopback-only.  Hermes 0.19 protects
        # this mode with X-Hermes-Session-Token and reports auth_required=false;
        # true denotes its separate remote OAuth/Cookie gate.  The protected
        # sessions request below proves the ephemeral token is accepted.
        if not version or status.get("auth_required") is not False:
            raise HermesProtocolError(
                "runtime Dashboard health payload is invalid",
                path="/api/status",
            )
        if not isinstance(sessions.get("sessions"), list):
            raise HermesProtocolError(
                "runtime Dashboard state store is unavailable",
                path="/api/sessions?limit=1",
            )
        return {
            "status": "ok",
            "version": version,
            "platform": "hermes-agent",
            "transport": "dashboard-ws",
            "http": "ready",
            "auth": "ready",
        }

    async def capabilities(self, endpoint: RuntimeEndpoint) -> dict[str, Any]:
        model, skills, toolsets, mcp, tool_inventory = await asyncio.gather(
            self._dashboard_json_request(
                endpoint,
                "GET",
                "/api/model/info",
            ),
            self._dashboard_json_value_request(
                endpoint,
                "GET",
                "/api/skills",
            ),
            self._dashboard_json_value_request(
                endpoint,
                "GET",
                "/api/tools/toolsets",
            ),
            self._dashboard_json_request(
                endpoint,
                "GET",
                "/api/mcp/servers",
            ),
            self.prewarm(endpoint),
        )
        if not isinstance(skills, list) or not all(
            isinstance(item, dict) for item in skills
        ):
            raise HermesProtocolError(
                "runtime Dashboard skills payload is invalid",
                path="/api/skills",
            )
        if not isinstance(toolsets, list) or not all(
            isinstance(item, dict) for item in toolsets
        ):
            raise HermesProtocolError(
                "runtime Dashboard toolsets payload is invalid",
                path="/api/tools/toolsets",
            )
        mcp_servers = mcp.get("servers")
        if not isinstance(mcp_servers, list) or not all(
            isinstance(item, dict) for item in mcp_servers
        ):
            raise HermesProtocolError(
                "runtime Dashboard MCP payload is invalid",
                path="/api/mcp/servers",
            )
        sections = tool_inventory.get("sections")
        if not isinstance(sections, list):
            raise HermesProtocolError(
                "runtime Dashboard tools.show payload is invalid"
            )
        resolved_tools: list[dict[str, Any]] = []
        for section in sections:
            rows = section.get("tools") if isinstance(section, dict) else None
            if not isinstance(rows, list) or not all(
                isinstance(row, dict) for row in rows
            ):
                raise HermesProtocolError(
                    "runtime Dashboard tools.show payload is invalid"
                )
            resolved_tools.extend(rows)
        if any(
            str(row.get(field) or "").strip().lower() == "cronjob"
            for row in resolved_tools
            for field in ("name", "toolset")
        ):
            raise HermesProtocolError(
                "runtime Dashboard still exposes the disabled cronjob toolset"
            )
        return {
            "object": "hermes.dashboard.capabilities",
            "platform": "hermes-agent",
            "runtime": {
                "mode": "dashboard_live_session",
                "tool_execution": "server",
                "split_runtime": False,
            },
            "model": model,
            "skills": skills,
            "toolsets": toolsets,
            "mcp_servers": mcp_servers,
            "resolved_tools": resolved_tools,
            "features": {
                "dashboard_http": True,
                "dashboard_ws": True,
                "session_resources": True,
                "session_chat_streaming": True,
                "session_model_switch": True,
                "run_stop": True,
                "approval_response": True,
                "artifact_streaming": True,
            },
        }

    async def dashboard_json_request(
        self,
        endpoint: RuntimeEndpoint,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Public management request seam used by compatibility adapters."""
        return await self._dashboard_json_request(
            endpoint,
            method,
            path,
            json_body=json_body,
        )

    async def reload_skills_and_list(self, endpoint: RuntimeEndpoint) -> list[dict[str, Any]]:
        """Reload Hermes Skills and return the post-reload discovery list.

        Plaza operations use this narrow protocol instead of the broader
        capabilities probe, so a Skill operation does not refresh unrelated
        MCP/tool inventories or active chat sessions.
        """
        await self.reload_skills(endpoint)
        return await self.list_skills(endpoint)

    async def list_skills(
        self,
        endpoint: RuntimeEndpoint,
    ) -> list[dict[str, Any]]:
        """Return the current Skill discovery list without probing other capabilities."""

        skills = await self._dashboard_json_value_request(
            endpoint,
            "GET",
            "/api/skills",
        )
        if not isinstance(skills, list) or not all(
            isinstance(item, dict) for item in skills
        ):
            raise HermesProtocolError(
                "runtime Dashboard skills payload is invalid",
                path="/api/skills",
            )
        return skills

    async def reload_skills(
        self,
        endpoint: RuntimeEndpoint,
    ) -> dict[str, Any]:
        """Request a native Skill reload and require an acknowledgement.

        Hermes 0.19 does not evict a previously discovered Skill from its
        live registry when its directory disappears.  Callers that only need
        the reload acknowledgement (notably uninstall) must therefore not
        infer failure from a subsequent stale ``/api/skills`` listing.
        """
        if self.connector is None:
            raise HermesProtocolError("runtime dashboard connector is unavailable")
        async with self.hot_channels.workspace_operation(
            endpoint.coordination_scope
        ):
            async with self.connector(endpoint) as websocket:
                result = await self._request(
                    websocket,
                    "skills.reload",
                    {},
                    [],
                    timeout_seconds=endpoint.timeout_seconds,
                )
        if not isinstance(result, dict):
            raise HermesProtocolError("runtime Dashboard skills.reload returned invalid result")
        return result

    async def reload_mcp_and_list(self, endpoint: RuntimeEndpoint) -> list[dict[str, Any]]:
        """Reload MCP configuration and return the post-reload registry.

        Callers that need installation verification can inspect the returned
        registry.  Uninstall callers should use :meth:`reload_mcp`: Hermes
        0.19 retains removed entries in this discovery list after a successful
        reload acknowledgement.
        """
        await self.reload_mcp(endpoint)
        payload = await self._dashboard_json_request(endpoint, "GET", "/api/mcp/servers")
        servers = payload.get("servers")
        if not isinstance(servers, list) or not all(isinstance(item, dict) for item in servers):
            raise HermesProtocolError("runtime Dashboard MCP payload is invalid", path="/api/mcp/servers")
        return servers

    async def reload_mcp_unconditional_and_list(
        self,
        endpoint: RuntimeEndpoint,
    ) -> list[dict[str, Any]]:
        await self._reload_mcp(endpoint, allow_active=True)
        return await self.list_mcp_registry_servers(endpoint)

    async def list_mcp_registry_servers(
        self,
        endpoint: RuntimeEndpoint,
    ) -> list[dict[str, Any]]:
        payload, inventory = await asyncio.gather(
            self._dashboard_json_request(
                endpoint,
                "GET",
                "/api/mcp/servers",
            ),
            self._list_registered_toolsets(endpoint),
        )
        servers = payload.get("servers")
        if not isinstance(servers, list) or not all(
            isinstance(item, dict) for item in servers
        ):
            raise HermesProtocolError(
                "runtime Dashboard MCP payload is invalid",
                path="/api/mcp/servers",
            )
        toolsets = inventory.get("toolsets")
        if not isinstance(toolsets, list) or not all(
            isinstance(item, dict) for item in toolsets
        ):
            raise HermesProtocolError(
                "runtime Dashboard tools.list payload is invalid",
            )
        by_name = {
            str(item.get("name") or "").strip(): item
            for item in toolsets
            if str(item.get("name") or "").strip()
        }
        observed: list[dict[str, Any]] = []
        for server in servers:
            identity = next(
                (
                    str(server.get(key) or "").strip()
                    for key in (
                        "slug",
                        "name",
                        "id",
                        "server_name",
                        "serverName",
                    )
                    if str(server.get(key) or "").strip()
                ),
                "",
            )
            toolset = by_name.get(f"mcp-{identity}") or by_name.get(identity)
            tools = toolset.get("tools") if toolset is not None else None
            observed.append(
                {
                    **server,
                    "tools": tools,
                    "toolset_available": (
                        toolset is not None
                    ),
                }
            )
        return observed

    async def _list_registered_toolsets(
        self,
        endpoint: RuntimeEndpoint,
    ) -> dict[str, Any]:
        if self.connector is None:
            raise HermesProtocolError(
                "runtime dashboard connector is unavailable"
            )
        async with self.connector(endpoint) as websocket:
            return await self._request(websocket, "tools.list", {}, [])

    async def list_profile_mcp_servers(
        self,
        endpoint: RuntimeEndpoint,
        *,
        profile_name: str,
    ) -> list[dict[str, Any]]:
        payload = await self._dashboard_json_request(
            endpoint,
            "GET",
            self._query_path(
                "/api/mcp/servers",
                [("profile", profile_name)],
            ),
        )
        servers = payload.get("servers")
        if not isinstance(servers, list) or not all(
            isinstance(item, dict) for item in servers
        ):
            raise HermesProtocolError(
                "runtime Dashboard Profile MCP payload is invalid",
                path="/api/mcp/servers",
            )
        return servers

    async def reload_mcp(self, endpoint: RuntimeEndpoint) -> None:
        """Reload MCP configuration and require Dashboard acknowledgement.

        The active-session safety rule is shared by installation and removal.
        Do not use a post-reload registry absence check for removal: Hermes
        0.19's discovery endpoint intentionally keeps stale removed entries.
        """
        await self._reload_mcp(
            endpoint,
            allow_active=self.allow_active_mcp_reload,
        )

    async def _reload_mcp(
        self,
        endpoint: RuntimeEndpoint,
        *,
        allow_active: bool,
    ) -> None:
        if self.connector is None:
            raise HermesProtocolError("runtime dashboard connector is unavailable")
        async with self.hot_channels.workspace_operation(
            endpoint.coordination_scope
        ):
            if not allow_active:
                await self.hot_channels.require_workspace_idle(
                    endpoint.coordination_scope
                )
            async with self.connector(endpoint) as websocket:
                buffered: list[dict[str, Any]] = []
                active = await self._request(
                    websocket,
                    "session.active_list",
                    {},
                    buffered,
                    timeout_seconds=endpoint.timeout_seconds,
                )
                raw_sessions = active.get("sessions")
                if not isinstance(raw_sessions, list) or not all(
                    isinstance(item, dict) for item in raw_sessions
                ):
                    raise HermesProtocolError(
                        "runtime dashboard session.active_list returned invalid sessions"
                    )
                session_ids: list[str] = []
                active_session_count = 0
                for item in raw_sessions:
                    state = str(item.get("status") or "").strip().lower()
                    if state != "idle":
                        if not allow_active:
                            raise HermesProtocolError(
                                "cannot reload Runtime MCP while dashboard session "
                                f"is {state or 'active'}"
                            )
                        active_session_count += 1
                        continue
                    session_id = str(item.get("id") or item.get("session_id") or "").strip()
                    if not session_id:
                        raise HermesProtocolError(
                            "runtime dashboard session.active_list returned a session without id"
                        )
                    session_ids.append(session_id)
                if active_session_count:
                    logger.warning(
                        "runtime_active_mcp_registry_reload "
                        "workspace_id=%s active_sessions=%s idle_sessions=%s",
                        endpoint.runtime_workspace_id,
                        active_session_count,
                        len(session_ids),
                    )
                await self._close_sessions_and_reload_mcp(
                    websocket,
                    buffered,
                    session_ids=session_ids,
                    timeout_seconds=endpoint.timeout_seconds,
                )

    async def _close_sessions_and_reload_mcp(
        self,
        websocket: DashboardWebSocket,
        buffered: list[dict[str, Any]],
        *,
        session_ids: list[str],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        # Hermes' MCP registry is process-global while each live Agent keeps a
        # Profile-scoped tool snapshot.  Reloading that global registry once
        # per live Agent resolves the Dashboard process Profile (normally
        # default) and silently replaces expert snapshots with default tools.
        # Close idle Agents first; the next Session prepare rebuilds each Agent
        # under its own Profile after the single global registry reload.
        for session_id in session_ids:
            closed = await self._request(
                websocket,
                "session.close",
                {"session_id": session_id},
                buffered,
                timeout_seconds=timeout_seconds,
            )
            if closed.get("closed") is not True:
                raise HermesProtocolError(
                    f"runtime dashboard session.close did not invalidate session {session_id}"
                )
        result = await self._request(
            websocket,
            "reload.mcp",
            {"confirm": True},
            buffered,
            timeout_seconds=timeout_seconds,
        )
        if result.get("status") != "reloaded":
            raise HermesProtocolError(
                "runtime dashboard reload.mcp did not reload registry"
            )
        return result

    async def prewarm(self, endpoint: RuntimeEndpoint) -> dict[str, Any]:
        if self.connector is None:
            raise HermesProtocolError("runtime dashboard connector is unavailable")
        async with self.connector(endpoint) as websocket:
            result = await self._request(websocket, "tools.show", {}, [])
        total = result.get("total")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise HermesProtocolError("runtime dashboard prewarm returned invalid tool metadata")
        return result

    async def initialize_default_session_store(
        self,
        endpoint: RuntimeEndpoint,
    ) -> dict[str, Any]:
        if self.connector is None:
            raise HermesProtocolError("runtime dashboard connector is unavailable")
        async with self.connector(endpoint) as websocket:
            result = await self._request(
                websocket,
                "session.list",
                {"limit": 1},
                [],
            )
        if not isinstance(result.get("sessions"), list):
            raise HermesProtocolError(
                "runtime Dashboard default session store is unavailable"
            )
        return result

    async def release_live_session(
        self,
        endpoint: RuntimeEndpoint,
        *,
        runtime_session_id: str,
    ) -> _DashboardLiveSessionRelease:
        """Release only this Session's idle live Agent, if one exists.

        Dashboard live sessions use an ephemeral ``id`` distinct from the
        durable Runtime Session key.  Looking up by the durable key first is
        what prevents a configuration refresh from closing a different Agent.
        A missing or already-closed Agent is an idempotent success; a working
        target is never interrupted by the preparation path.
        """

        if self.connector is None:
            raise HermesProtocolError("runtime dashboard connector is unavailable")
        async with self.connector(endpoint) as websocket:
            buffered: list[dict[str, Any]] = []
            active = await self._request(
                websocket,
                "session.active_list",
                {},
                buffered,
                timeout_seconds=endpoint.timeout_seconds,
            )
            raw_sessions = active.get("sessions")
            if not isinstance(raw_sessions, list) or not all(
                isinstance(item, dict) for item in raw_sessions
            ):
                raise HermesProtocolError(
                    "runtime dashboard session.active_list returned invalid sessions"
                )
            matches = [
                item
                for item in raw_sessions
                if str(item.get("session_key") or "").strip()
                == runtime_session_id
            ]
            if not matches:
                return self._live_session_release(runtime_session_id)
            if len(matches) != 1:
                raise HermesProtocolError(
                    "runtime dashboard returned duplicate live sessions for the session key"
                )
            matched = matches[0]
            status = str(matched.get("status") or "").strip().lower()
            if status != "idle":
                raise HermesProtocolError(
                    "runtime dashboard live session is still working"
                )
            live_session_id = str(matched.get("id") or "").strip()
            if not live_session_id:
                raise HermesProtocolError(
                    "runtime dashboard live session is missing its live id"
                )
            result = await self._request(
                websocket,
                "session.close",
                {"session_id": live_session_id},
                buffered,
                timeout_seconds=endpoint.timeout_seconds,
            )
            closed = result.get("closed")
            if not isinstance(closed, bool):
                raise HermesProtocolError(
                    "runtime dashboard session.close returned invalid result"
                )
        return self._live_session_release(runtime_session_id)

    def issue_route_prepare_proof(
        self,
        release: object,
        *,
        runtime_session_id: str,
        hermes_profile_name: str,
        expected_model: str,
        expected_provider: str,
    ) -> DashboardSessionRouteProof:
        """Bind a completed live release to the route just prepared by Runtime.

        The opaque authority makes this evidence process-local: JSON requests,
        Backend grants and Dashboard responses cannot provide or restore it.
        The ExecutionAdapter calls this only after route registration and the
        durable SessionDB import/repair both complete.
        """

        if (
            not isinstance(release, _DashboardLiveSessionRelease)
            or release._authority is not _LIVE_SESSION_RELEASE_AUTHORITY
            or release.runtime_session_id != runtime_session_id
        ):
            raise HermesProtocolError(
                "runtime dashboard live execution release proof is invalid"
            )
        model = str(expected_model or "").strip()
        provider = str(expected_provider or "").strip()
        profile = str(hermes_profile_name or "default").strip() or "default"
        if not model or not provider:
            raise HermesProtocolError(
                "runtime dashboard session route proof is incomplete"
            )
        return DashboardSessionRouteProof(
            runtime_session_id=runtime_session_id,
            hermes_profile_name=profile,
            expected_model=model,
            expected_provider=provider,
            _authority=_ROUTE_PREPARE_PROOF_AUTHORITY,
        )

    @staticmethod
    def _live_session_release(
        runtime_session_id: str,
    ) -> _DashboardLiveSessionRelease:
        return _DashboardLiveSessionRelease(
            runtime_session_id=runtime_session_id,
            _authority=_LIVE_SESSION_RELEASE_AUTHORITY,
        )

    async def prewarm_session(
        self,
        endpoint: RuntimeEndpoint,
        *,
        runtime_session_id: str,
        hermes_profile_name: str = "default",
        runtime_agent_profile_id: int | None = None,
        hot_channel_identity: DashboardHotChannelIdentity | None = None,
        expected_provider: str | None = None,
        route_prepare_proof: DashboardSessionRouteProof | None = None,
        selected_only: bool = False,
        selection_epoch: int | None = None,
        eager_build: bool = False,
    ) -> dict[str, Any]:
        if self.connector is None:
            raise HermesProtocolError("runtime dashboard connector is unavailable")
        if hermes_profile_name != "default" and hot_channel_identity is None:
            raise HermesProtocolError(
                "runtime dashboard expert prewarm requires a HotChannel identity"
            )
        if hot_channel_identity is not None:
            async def prepare(peer: DashboardRpcPeer) -> DashboardPreparedPeer:
                return await self._prepare_hot_peer(
                    peer,
                    runtime_session_id=runtime_session_id,
                    hermes_profile_name=hermes_profile_name,
                    runtime_agent_profile_id=runtime_agent_profile_id,
                    expected_model=_hot_channel_expected_model(
                        hot_channel_identity
                    ),
                    timeout_seconds=endpoint.timeout_seconds,
                    eager_build=eager_build,
                    expected_provider=expected_provider,
                    route_prepare_proof=route_prepare_proof,
                )

            async def wait_ready(
                peer: DashboardRpcPeer,
                live_session_id: str,
            ) -> None:
                await self._wait_hot_peer_ready(
                    peer,
                    live_session_id,
                    timeout_seconds=endpoint.timeout_seconds,
                )

            for attempt in range(1, self.prewarm_attempts + 1):
                try:
                    prewarmed = await self.hot_channels.prewarm(
                        hot_channel_identity,
                        endpoint,
                        self.connector,
                        prepare,
                        wait_ready=wait_ready,
                        selected_only=selected_only,
                        selection_epoch=selection_epoch,
                    )
                    if prewarmed is False:
                        return {
                            "session_id": runtime_session_id,
                            "superseded": True,
                        }
                    break
                except DashboardHotCapacityUnavailable:
                    return {
                        "session_id": runtime_session_id,
                        "capacity_fallback": True,
                    }
                except HermesProtocolError:
                    if attempt >= self.prewarm_attempts:
                        raise
                    logger.info(
                        "dashboard session prewarm retry workspace_id=%s "
                        "session_id=%s attempt=%s",
                        endpoint.runtime_workspace_id,
                        runtime_session_id,
                        attempt + 1,
                    )
                    if self.prewarm_backoff_seconds > 0:
                        await asyncio.sleep(
                            self.prewarm_backoff_seconds * (2 ** (attempt - 1))
                        )
            return {"session_id": runtime_session_id}
        async with self.connector(endpoint) as websocket:
            resume_params: dict[str, Any] = {
                "session_id": runtime_session_id,
                "cols": 96,
                "eager_build": True,
                "omit_messages": True,
            }
            if hermes_profile_name != "default":
                resume_params["profile"] = hermes_profile_name
            result = await self._request(
                websocket,
                "session.resume",
                resume_params,
                [],
            )
            self._validate_omitted_resume(result)
        if not isinstance(result.get("session_id"), str) or not result["session_id"].strip():
            raise HermesProtocolError("runtime dashboard prewarm returned invalid live session")
        return result

    async def apply_config(self, endpoint: RuntimeEndpoint, **kwargs: Any) -> dict[str, Any]:
        if self.connector is None:
            raise HermesProtocolError("runtime dashboard connector is unavailable")
        generation = kwargs.get("generation")
        expected_projection_hash = kwargs.get("expected_projection_hash")
        if not isinstance(generation, int) or not isinstance(expected_projection_hash, str):
            raise HermesProtocolError("runtime dashboard apply requires generation and expected_projection_hash")
        reload_skills = bool(kwargs.get("reload_skills", True))
        reload_mcp = bool(kwargs.get("reload_mcp", True))
        async with self.hot_channels.workspace_operation(
            endpoint.coordination_scope
        ):
            if reload_mcp:
                await self.hot_channels.require_workspace_idle(
                    endpoint.coordination_scope
                )
            return await self._apply_config_locked(
                endpoint,
                generation=generation,
                expected_projection_hash=expected_projection_hash,
                reload_skills=reload_skills,
                reload_mcp=reload_mcp,
                inspect_live_sessions=reload_mcp,
            )

    async def _apply_config_locked(
        self,
        endpoint: RuntimeEndpoint,
        *,
        generation: int,
        expected_projection_hash: str,
        reload_skills: bool,
        reload_mcp: bool,
        inspect_live_sessions: bool,
    ) -> dict[str, Any]:
        assert self.connector is not None
        reload_results: dict[str, Any] = {}
        async with self.connector(endpoint) as websocket:
            buffered: list[dict[str, Any]] = []
            active_session_ids: list[str] = []
            if inspect_live_sessions:
                active = await self._request(
                    websocket,
                    "session.active_list",
                    {},
                    buffered,
                    timeout_seconds=endpoint.timeout_seconds,
                )
                raw_sessions = active.get("sessions")
                if not isinstance(raw_sessions, list) or not all(
                    isinstance(item, dict) for item in raw_sessions
                ):
                    raise HermesProtocolError(
                        "runtime dashboard session.active_list returned invalid sessions"
                    )
                for item in raw_sessions:
                    status = str(item.get("status") or "").strip().lower()
                    if status != "idle":
                        raise HermesProtocolError(
                            "cannot reload Runtime config while dashboard session "
                            f"is {status or 'active'}"
                        )
                    session_id = str(
                        item.get("id") or item.get("session_id") or ""
                    ).strip()
                    if not session_id:
                        raise HermesProtocolError(
                            "runtime dashboard session.active_list returned a session without id"
                        )
                    active_session_ids.append(session_id)

            if reload_skills:
                logger.debug(
                    "dashboard rpc skills.reload start workspace_id=%s",
                    endpoint.runtime_workspace_id,
                )
                reload_results["skills"] = await self._request(
                    websocket,
                    "skills.reload",
                    {},
                    buffered,
                    timeout_seconds=endpoint.timeout_seconds,
                )
                logger.debug(
                    "dashboard rpc skills.reload done workspace_id=%s",
                    endpoint.runtime_workspace_id,
                )
            if reload_mcp:
                logger.debug(
                    "dashboard rpc reload.mcp start workspace_id=%s",
                    endpoint.runtime_workspace_id,
                )
                reload_results["mcp"] = await self._close_sessions_and_reload_mcp(
                    websocket,
                    buffered,
                    session_ids=active_session_ids,
                    timeout_seconds=endpoint.timeout_seconds,
                )
                reload_results["mcp_sessions"] = active_session_ids
                logger.debug(
                    "dashboard rpc reload.mcp done workspace_id=%s",
                    endpoint.runtime_workspace_id,
                )
        return {
            "loaded_generation": generation,
            "projection_hash": expected_projection_hash,
            "reload": reload_results,
        }

    async def create_session(self, endpoint: RuntimeEndpoint, **kwargs: Any) -> dict[str, Any]:
        runtime_session_id = str(kwargs.get("runtime_session_id") or "").strip()
        if not runtime_session_id:
            raise HermesProtocolError("runtime session id is required")
        title = kwargs.get("title")
        model = kwargs.get("model")
        provider = str(kwargs.get("provider") or "").strip()
        hermes_profile_name = str(kwargs.get("hermes_profile_name") or "default")
        session: dict[str, Any] = {
            "id": runtime_session_id,
            # Hermes treats ``api_server`` as TUI-owned: native
            # ``session.close`` marks that durable row ended, so an MCP reload
            # cannot rebuild the live Agent under the same Runtime Session id.
            # Supervisor is the external execution owner, which matches the
            # native gateway ``relay`` lifecycle and keeps close limited to the
            # ephemeral Agent.
            "source": "relay",
            "messages": [],
            # Hermes keeps a UNIQUE index on sessions.title. Runtime session
            # titles are user-facing and are allowed to repeat, so keep the
            # Hermes-internal title deterministic and unique without changing
            # the authoritative title stored by Backend.
            "title": self._internal_session_title(runtime_session_id, title),
        }
        if model is not None:
            session["model"] = str(model)
        # A Runtime route is per-session and ephemeral.  Store its canonical
        # Hermes provider identity with the imported execution session so that
        # ``session.resume`` builds the first Agent directly against the
        # registered Relay route.  Deferring this to ``config.set`` builds an
        # Agent using the global platform provider first, then performs a live
        # model switch; Hermes correctly records that switch as a visible
        # ``display_kind=model_switch`` history message.  The Dashboard import
        # contract natively persists model_config and restores it before the
        # lazy Agent build, so no Hermes-core change or history filtering is
        # required.
        if provider:
            session["model_config"] = {
                "model": str(model or "").strip(),
                "provider": provider,
            }
        # Do not persist the managed session directory as ``cwd`` while the
        # session is merely imported.  At this point the directory may not
        # exist yet; Dashboard initializes an Agent during ``session.resume``
        # and rejects a saved nonexistent cwd before Backend gets a chance to
        # create it.  The first chat/prewarm path creates the directory and
        # sets cwd explicitly before it submits a prompt.
        import_body: dict[str, Any] = {"sessions": [session]}
        if hermes_profile_name != "default":
            import_body["profile"] = hermes_profile_name
        payload = await self._dashboard_json_request(
            endpoint,
            "POST",
            "/api/sessions/import",
            json_body=import_body,
        )
        imported_ids = payload.get("imported_ids")
        skipped_ids = payload.get("skipped_ids")
        if (
            payload.get("ok") is not True
            or not isinstance(imported_ids, list)
            or not isinstance(skipped_ids, list)
            or runtime_session_id not in {*imported_ids, *skipped_ids}
            or int(payload.get("imported") or 0)
            + int(payload.get("skipped") or 0)
            != 1
        ):
            raise HermesProtocolError(
                "runtime dashboard session import returned invalid result",
                path="/api/sessions/import",
            )
        if runtime_session_id in skipped_ids and model is not None and provider:
            # The Dashboard import API deliberately skips duplicate IDs.  That
            # is correct for conversation history, but sessions created before
            # Runtime began persisting the upstream model identity can retain
            # the platform-only ID (for example ``19``), and legacy
            # ``api_server`` rows can have been ended by native
            # ``session.close``.  Reopen and repair only the durable execution
            # metadata through Hermes' public SessionDB API before
            # ``session.resume`` builds an Agent.  Unlike ``config.set`` this
            # does not create a visible model-switch message in the user's
            # conversation history.
            await self._repair_existing_session_model(
                runtime_session_id=runtime_session_id,
                model=str(model).strip(),
                provider=provider,
                hermes_profile_name=hermes_profile_name,
            )
        return {
            "session": {
                "id": runtime_session_id,
                "model": model,
                "provider": provider or None,
                "title": title,
            }
        }

    async def _repair_existing_session_model(
        self,
        *,
        runtime_session_id: str,
        model: str,
        provider: str,
        hermes_profile_name: str,
        refresh_system_prompt: bool = False,
    ) -> None:
        await asyncio.to_thread(
            self._repair_existing_session_model_blocking,
            runtime_session_id,
            model,
            provider,
            hermes_profile_name,
            refresh_system_prompt,
        )

    async def refresh_session_profile(
        self,
        endpoint: RuntimeEndpoint,
        *,
        runtime_session_id: str,
        model: str,
        provider: str,
        hermes_profile_name: str,
    ) -> None:
        del endpoint
        await self._repair_existing_session_model(
            runtime_session_id=runtime_session_id,
            model=model,
            provider=provider,
            hermes_profile_name=hermes_profile_name,
            refresh_system_prompt=True,
        )

    @staticmethod
    def _repair_existing_session_model_blocking(
        runtime_session_id: str,
        model: str,
        provider: str,
        hermes_profile_name: str,
        refresh_system_prompt: bool = False,
    ) -> None:
        home = os.environ.get("HERMES_HOME") or "/opt/data"
        request: dict[str, object] = {
            "contract_version": (
                "hermes-session-metadata-repair-v2"
                if refresh_system_prompt
                else "hermes-session-metadata-repair-v1"
            ),
            "runtime_session_id": runtime_session_id,
            "model": model,
            "provider": provider,
            "hermes_profile_name": hermes_profile_name,
        }
        if refresh_system_prompt:
            request["refresh_system_prompt"] = True
        environment = {
            "HOME": home,
            "HERMES_HOME": home,
            "HERMES_SKIP_CONFIG_MIGRATION": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
        try:
            completed = subprocess.run(
                [
                    _HERMES_CHILD_EXECUTABLE,
                    "--uid",
                    "10001",
                    "--gid",
                    "10001",
                    "--supplemental-gid",
                    "1000",
                    "--",
                    _HERMES_PYTHON,
                    _SESSION_METADATA_HELPER,
                ],
                input=json.dumps(
                    request,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                timeout=30,
                check=False,
            )
        except Exception as exc:
            raise HermesProtocolError(
                "runtime Dashboard execution-session metadata repair failed"
            ) from exc
        if completed.returncode != 0 or completed.stdout != b'{"ok":true}':
            raise HermesProtocolError(
                "runtime Dashboard execution-session metadata repair failed"
            )

    @staticmethod
    def _query_path(
        path: str,
        params: list[tuple[str, str | int]],
    ) -> str:
        query = urlencode(params)
        return f"{path}?{query}" if query else path

    @classmethod
    def _session_messages_path(
        cls,
        *,
        runtime_session_id: str,
        hermes_profile_name: str,
        limit: int,
        offset: int,
        order: str,
    ) -> str:
        params: list[tuple[str, str | int]] = []
        if hermes_profile_name != "default":
            params.append(("profile", hermes_profile_name))
        params.extend(
            [
                ("limit", limit),
                ("offset", offset),
                ("order", order),
            ]
        )
        return cls._query_path(
            f"/api/sessions/{quote(runtime_session_id, safe='')}/messages",
            params,
        )

    @staticmethod
    def _validate_message_page(
        payload: dict[str, Any],
        *,
        expected_limit: int,
        expected_offset: int,
        expected_order: str,
        path: str,
    ) -> tuple[str, list[dict[str, Any]], int, int | None]:
        session_id = payload.get("session_id")
        messages = payload.get("messages")
        pagination = payload.get("pagination")
        if not isinstance(session_id, str) or not session_id.strip():
            raise HermesProtocolError(
                "runtime Dashboard message page returned invalid session lineage",
                path=path,
            )
        if not isinstance(messages, list) or not all(
            isinstance(item, dict) for item in messages
        ):
            raise HermesProtocolError(
                "runtime Dashboard message page returned invalid messages",
                path=path,
            )
        if not isinstance(pagination, dict):
            raise HermesProtocolError(
                "runtime Dashboard message pagination is missing",
                path=path,
            )

        def integer(name: str) -> int:
            value = pagination.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HermesProtocolError(
                    f"runtime Dashboard message pagination.{name} is invalid",
                    path=path,
                )
            return value

        page_limit = integer("limit")
        page_offset = integer("offset")
        returned = integer("returned")
        page_order = pagination.get("order")
        if (
            page_limit != expected_limit
            or page_offset != expected_offset
            or page_order != expected_order
            or returned != len(messages)
            or returned > expected_limit
        ):
            raise HermesProtocolError(
                "runtime Dashboard message pagination does not match the request",
                path=path,
            )
        raw_total = pagination.get("total")
        total: int | None = None
        if raw_total is not None:
            if (
                isinstance(raw_total, bool)
                or not isinstance(raw_total, int)
                or raw_total < 0
                or expected_offset + returned > raw_total
            ):
                raise HermesProtocolError(
                    "runtime Dashboard message pagination.total is invalid",
                    path=path,
                )
            total = raw_total
        return session_id.strip(), messages, returned, total

    async def _read_session_message_pages(
        self,
        endpoint: RuntimeEndpoint,
        *,
        runtime_session_id: str,
        hermes_profile_name: str,
        order: str,
        initiating_user_message: str | None = None,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        offset = 0
        resolved_session_id: str | None = None
        expected_total: int | None = None
        total_presence: bool | None = None
        page_digests: set[str] = set()
        messages: list[dict[str, Any]] = []
        while True:
            path = self._session_messages_path(
                runtime_session_id=runtime_session_id,
                hermes_profile_name=hermes_profile_name,
                limit=_DASHBOARD_MESSAGE_PAGE_SIZE,
                offset=offset,
                order=order,
            )
            payload = await self._dashboard_json_request(
                endpoint,
                "GET",
                path,
            )
            page_session_id, page, returned, total = self._validate_message_page(
                payload,
                expected_limit=_DASHBOARD_MESSAGE_PAGE_SIZE,
                expected_offset=offset,
                expected_order=order,
                path=path,
            )
            if resolved_session_id is None:
                resolved_session_id = page_session_id
            elif page_session_id != resolved_session_id:
                raise _DashboardSessionLineageChanged

            page_has_total = total is not None
            if total_presence is None:
                total_presence = page_has_total
                expected_total = total
            elif total_presence != page_has_total or (
                page_has_total and total != expected_total
            ):
                raise HermesProtocolError(
                    "runtime Dashboard message pagination total changed between pages",
                    path=path,
                )

            if page:
                page_digest = hashlib.sha256(
                    json.dumps(
                        page,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if page_digest in page_digests:
                    raise HermesProtocolError(
                        "runtime Dashboard repeated a message pagination page",
                        path=path,
                    )
                page_digests.add(page_digest)
                if order == "latest":
                    messages[0:0] = page
                else:
                    messages.extend(page)

            if (
                order == "latest"
                and initiating_user_message is not None
                and self._completed_turn_text(
                    messages,
                    initiating_user_message,
                    workspace=workspace,
                )
                is not None
            ):
                break

            next_offset = offset + returned
            if returned == 0:
                if expected_total is not None and offset < expected_total:
                    raise HermesProtocolError(
                        "runtime Dashboard message pagination made no progress",
                        path=path,
                    )
                break
            if expected_total is not None:
                if next_offset == expected_total:
                    break
                if returned < _DASHBOARD_MESSAGE_PAGE_SIZE:
                    raise HermesProtocolError(
                        "runtime Dashboard message pagination ended before total",
                        path=path,
                    )
            elif returned < _DASHBOARD_MESSAGE_PAGE_SIZE:
                break
            offset = next_offset

        if resolved_session_id is None:
            raise HermesProtocolError(
                "runtime Dashboard message history returned no session lineage"
            )
        return {"session_id": resolved_session_id, "messages": messages}

    async def _read_stable_session_messages(
        self,
        endpoint: RuntimeEndpoint,
        **kwargs: Any,
    ) -> dict[str, Any]:
        for attempt in range(2):
            try:
                return await self._read_session_message_pages(endpoint, **kwargs)
            except _DashboardSessionLineageChanged:
                if attempt == 1:
                    raise HermesProtocolError(
                        "runtime Dashboard session lineage changed repeatedly during pagination"
                    ) from None
        raise AssertionError("unreachable")

    async def get_session_messages(
        self,
        endpoint: RuntimeEndpoint,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await self._read_stable_session_messages(
            endpoint,
            runtime_session_id=str(kwargs.get("runtime_session_id") or ""),
            hermes_profile_name=str(
                kwargs.get("hermes_profile_name") or "default"
            ),
            order="oldest",
        )

    async def _get_latest_session_messages(
        self,
        endpoint: RuntimeEndpoint,
        *,
        runtime_session_id: str,
        hermes_profile_name: str,
        initiating_user_message: str,
        workspace: str | None,
    ) -> dict[str, Any]:
        return await self._read_stable_session_messages(
            endpoint,
            runtime_session_id=runtime_session_id,
            hermes_profile_name=hermes_profile_name,
            order="latest",
            initiating_user_message=initiating_user_message,
            workspace=workspace,
        )

    async def list_sessions(
        self,
        endpoint: RuntimeEndpoint,
        *,
        limit: int,
        offset: int = 0,
    ) -> dict[str, Any]:
        requested_limit = max(1, min(int(limit), 500))
        requested_offset = max(0, int(offset))
        base_sessions: list[dict[str, Any]] = []
        base_ids: set[str] = set()
        pinned_outside: dict[str, dict[str, Any]] = {}
        upstream_offset = requested_offset
        remaining = requested_limit
        expected_total: int | None = None
        while remaining > 0:
            page_limit = min(_DASHBOARD_SESSION_LIST_PAGE_SIZE, remaining)
            path = self._query_path(
                "/api/sessions",
                [("limit", page_limit), ("offset", upstream_offset)],
            )
            payload = await self._dashboard_json_request(endpoint, "GET", path)
            sessions = payload.get("sessions")
            total = payload.get("total")
            page_response_limit = payload.get("limit")
            page_response_offset = payload.get("offset")
            if (
                not isinstance(sessions, list)
                or not all(isinstance(item, dict) for item in sessions)
                or isinstance(total, bool)
                or not isinstance(total, int)
                or total < 0
                or isinstance(page_response_limit, bool)
                or page_response_limit != page_limit
                or isinstance(page_response_offset, bool)
                or page_response_offset != upstream_offset
            ):
                raise HermesProtocolError(
                    "runtime Dashboard session list pagination is invalid",
                    path=path,
                )
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise HermesProtocolError(
                    "runtime Dashboard session list total changed between pages",
                    path=path,
                )

            base_count = min(page_limit, max(0, total - upstream_offset))
            if len(sessions) < base_count:
                raise HermesProtocolError(
                    "runtime Dashboard session list page ended before total",
                    path=path,
                )
            page_base = sessions[:base_count]
            page_pinned = sessions[base_count:]
            for session in page_base:
                session_id = session.get("id")
                if not isinstance(session_id, str) or not session_id.strip():
                    raise HermesProtocolError(
                        "runtime Dashboard session list row has no id",
                        path=path,
                    )
                session_id = session_id.strip()
                pinned_outside.pop(session_id, None)
                if session_id not in base_ids:
                    base_ids.add(session_id)
                    base_sessions.append(session)
            for session in page_pinned:
                session_id = session.get("id")
                if (
                    not isinstance(session_id, str)
                    or not session_id.strip()
                    or session.get("pinned") is not True
                ):
                    raise HermesProtocolError(
                        "runtime Dashboard session list returned invalid pinned rows",
                        path=path,
                    )
                session_id = session_id.strip()
                if session_id not in base_ids:
                    pinned_outside.setdefault(session_id, session)

            upstream_offset += page_limit
            remaining -= page_limit
            if upstream_offset >= total:
                break

        return {
            "sessions": [*base_sessions, *pinned_outside.values()],
            "total": expected_total or 0,
            "limit": requested_limit,
            "offset": requested_offset,
        }

    async def rename_session(self, endpoint: RuntimeEndpoint, **kwargs: Any) -> dict[str, Any]:
        runtime_session_id = str(kwargs.get("runtime_session_id") or "")
        title = str(kwargs.get("title") or "")
        hermes_profile_name = str(kwargs.get("hermes_profile_name") or "default")
        rename_body: dict[str, Any] = {
            "title": self._internal_session_title(runtime_session_id, title)
        }
        if hermes_profile_name != "default":
            rename_body["profile"] = hermes_profile_name
        await self._dashboard_json_request(
            endpoint,
            "PATCH",
            f"/api/sessions/{quote(runtime_session_id, safe='')}",
            json_body=rename_body,
        )
        return {
            "session": {
                "id": runtime_session_id,
                "title": title,
            }
        }

    @staticmethod
    def _internal_session_title(runtime_session_id: str, title: object) -> str:
        visible_title = str(title or "新会话").strip() or "新会话"
        return f"{visible_title} [{runtime_session_id}]"

    async def delete_session(self, endpoint: RuntimeEndpoint, **kwargs: Any) -> dict[str, Any]:
        runtime_session_id = quote(
            str(kwargs.get("runtime_session_id") or ""),
            safe="",
        )
        hermes_profile_name = str(
            kwargs.get("hermes_profile_name") or "default"
        )
        delete_path = f"/api/sessions/{runtime_session_id}"
        if hermes_profile_name != "default":
            delete_path = self._query_path(
                delete_path,
                [("profile", hermes_profile_name)],
            )
        return await self._dashboard_json_request(
            endpoint,
            "DELETE",
            delete_path,
        )

    @staticmethod
    def _validate_omitted_resume(resumed: dict[str, Any]) -> None:
        message_count = resumed.get("message_count")
        if (
            resumed.get("messages_omitted") is not True
            or resumed.get("messages") != []
            or isinstance(message_count, bool)
            or not isinstance(message_count, int)
            or message_count < 0
        ):
            raise HermesProtocolError(
                "runtime dashboard session.resume omit_messages response is invalid",
                rpc_method="session.resume",
            )

    async def stream_session_chat(
        self,
        endpoint: RuntimeEndpoint,
        *,
        runtime_session_id: str,
        message: str,
        attachments: (
            list[dict[str, object]] | tuple[dict[str, object], ...]
        ) = (),
        instructions: str | None = None,
        expected_model: str | None = None,
        expected_provider: str | None = None,
        route_prepare_proof: DashboardSessionRouteProof | None = None,
        hermes_profile_name: str = "default",
        runtime_agent_profile_id: int | None = None,
        runtime_run_id: str | None = None,
        hot_channel_identity: DashboardHotChannelIdentity | None = None,
        timing: Any | None = None,
        trusted_context: dict[str, Any] | None = None,
        platform_contract_version: int | None = None,
        platform_contract_digest: str | None = None,
        include_platform_contract: bool = True,
        prompt_submit_started_sink: (
            Callable[[], Awaitable[None] | None] | None
        ) = None,
        prompt_ack_sink: Callable[[], Awaitable[None]] | None = None,
        runtime_provider_run_id_sink: (
            Callable[[str], Awaitable[None]] | None
        ) = None,
    ):
        del instructions  # Dashboard resumes the canonical state.db session.
        if self.connector is None:
            if timing is not None:
                _log_dashboard_timing(
                    timing,
                    stage="failed",
                    status="error",
                    error_type="HermesProtocolError",
                )
            raise HermesProtocolError("runtime dashboard connector is unavailable")
        prompt_attempted = False
        streamed_text = ""
        streamed_reasoning = ""
        progress_markers = ProgressMarkerBuffer()
        reasoning_streamed = False
        latest_progress_label: str | None = None
        parked = False
        terminal = False
        reconnect_count = 0
        workspace = self._session_workspace_path(
            runtime_session_id,
            hermes_profile_name=hermes_profile_name,
            runtime_agent_profile_id=runtime_agent_profile_id,
        )
        hot_attempted = False
        pre_prompt_timeout_seconds = (
            endpoint.timeout_seconds
            if hot_channel_identity is not None
            else None
        )
        submit_status: str | None = None
        turn_gate_open = False
        reported_runtime_provider_run_id: str | None = None
        attached_image_paths: set[str] = set()

        while not terminal:
            use_hot_this_attempt = not hot_attempted
            try:
                async with self._open_chat_connection(
                    endpoint,
                    runtime_session_id=runtime_session_id,
                    runtime_run_id=runtime_run_id,
                    hermes_profile_name=hermes_profile_name,
                    runtime_agent_profile_id=runtime_agent_profile_id,
                    expected_model=expected_model,
                    expected_provider=expected_provider,
                    route_prepare_proof=route_prepare_proof,
                    hot_channel_identity=hot_channel_identity,
                    use_hot=use_hot_this_attempt,
                    timing=timing,
                ) as connection:
                    hot_attempted = True
                    if timing is not None:
                        timing.mark("dashboard_connection_acquired")
                    websocket = connection.websocket
                    buffered: list[dict[str, Any]] = []
                    preparation_timeout_seconds = (
                        pre_prompt_timeout_seconds
                        if not prompt_attempted
                        else None
                    )
                    if connection.live_session_id is None:
                        if workspace:
                            await self._ensure_session_workspace(
                                websocket,
                                buffered,
                                runtime_session_id=runtime_session_id,
                                hermes_profile_name=hermes_profile_name,
                                runtime_agent_profile_id=runtime_agent_profile_id,
                                timeout_seconds=preparation_timeout_seconds,
                            )
                            if timing is not None:
                                timing.mark("workspace_ready")
                        resume_params: dict[str, Any] = {
                            "session_id": runtime_session_id,
                            "cols": 96,
                        }
                        if not prompt_attempted:
                            resume_params["omit_messages"] = True
                        if hermes_profile_name != "default":
                            resume_params["profile"] = hermes_profile_name
                        resumed = await self._request(
                            websocket,
                            "session.resume",
                            resume_params,
                            buffered,
                            timeout_seconds=preparation_timeout_seconds,
                        )
                        if resume_params.get("omit_messages") is True:
                            self._validate_omitted_resume(resumed)
                        if timing is not None:
                            timing.mark("session_resumed")
                        live_session_id = str(resumed.get("session_id") or "")
                    else:
                        live_session_id = connection.live_session_id
                        # The hot-channel identity already includes the effective
                        # model ID. Reusing that exact identity means prewarm built
                        # the requested model even though resume metadata is not
                        # replayed through the lease.
                        resumed = {
                            "session_id": live_session_id,
                            "info": {
                                "model": (
                                    connection.model
                                    or str(expected_model or "").strip()
                                ),
                                "provider": connection.provider,
                            },
                        }
                    if not live_session_id:
                        raise HermesProtocolError("runtime dashboard did not return a live session id")
                    if connection.control_lease is not None:
                        await connection.control_lease.bind_live_session(
                            live_session_id
                        )
                    if (
                        runtime_provider_run_id_sink is not None
                        and live_session_id
                        != reported_runtime_provider_run_id
                    ):
                        await runtime_provider_run_id_sink(live_session_id)
                        reported_runtime_provider_run_id = live_session_id

                    if not prompt_attempted:
                        expert_hot_channel = (
                            hermes_profile_name != "default"
                            and connection.hot_lease is not None
                        )
                        if expert_hot_channel:
                            if timing is not None:
                                timing.set_metadata(
                                    "provider_calibration",
                                    "hot_hit",
                                )
                        else:
                            await self._ensure_session_model(
                                websocket,
                                buffered,
                                runtime_session_id=runtime_session_id,
                                resumed=resumed,
                                expected_model=expected_model,
                                force_platform_provider=(
                                    hermes_profile_name != "default"
                                    or bool(expected_provider)
                                ),
                                expected_provider=expected_provider,
                                expected_profile=hermes_profile_name,
                                route_prepare_proof=route_prepare_proof,
                            )
                            if timing is not None:
                                timing.mark("model_ready")
                            if (
                                timing is not None
                                and hermes_profile_name != "default"
                            ):
                                timing.set_metadata(
                                    "provider_calibration",
                                    "cold_fallback",
                                )
                        if connection.workspace is not None:
                            workspace = connection.workspace
                        elif workspace:
                            workspace = await self._prepare_session_workspace(
                                websocket,
                                buffered,
                                live_session_id=live_session_id,
                                runtime_session_id=runtime_session_id,
                                hermes_profile_name=hermes_profile_name,
                                runtime_agent_profile_id=runtime_agent_profile_id,
                                ensure_exists=False,
                                timeout_seconds=preparation_timeout_seconds,
                            )
                            if timing is not None:
                                timing.mark("cwd_ready")
                        validated_attachments = [
                            RuntimeTurnAttachment.model_validate(item)
                            for item in attachments
                        ]
                        prompt_params = {
                            "session_id": live_session_id,
                            "text": self._message_with_platform_contract(
                                message,
                                workspace,
                                trusted_context=trusted_context,
                                platform_contract_version=(
                                    platform_contract_version
                                ),
                                platform_contract_digest=(
                                    platform_contract_digest
                                ),
                                include_platform_contract=(
                                    include_platform_contract
                                ),
                            ),
                            # A Session FIFO Run is never a correction of the
                            # active turn.  When Hermes is still finishing that
                            # turn, this forces the text into its next-turn
                            # queue instead of redirect/steer semantics.
                            "queued": True,
                        }
                        prompt_attempted = True
                        # Notifications collected by resume/model/workspace RPCs
                        # predate this prompt and cannot belong to its turn.
                        buffered.clear()
                        if timing is not None:
                            timing.mark("prompt_submit_begin")
                        if prompt_submit_started_sink is not None:
                            started = prompt_submit_started_sink()
                            if inspect.isawaitable(started):
                                await started
                        for attachment in validated_attachments:
                            mime_type = str(attachment.mime_type or "")
                            if not mime_type.startswith("image/"):
                                continue
                            absolute_path = self._managed_absolute_path(
                                attachment.relative_path
                            )
                            if absolute_path in attached_image_paths:
                                continue
                            attached = await self._request(
                                websocket,
                                "image.attach",
                                {
                                    "session_id": live_session_id,
                                    "path": absolute_path,
                                },
                                buffered,
                                timeout_seconds=endpoint.timeout_seconds,
                            )
                            if attached.get("attached") is not True:
                                raise HermesProtocolError(
                                    "runtime dashboard image attachment failed",
                                    rpc_method="image.attach",
                                    rpc_data={
                                        "file_id": attachment.file_id,
                                    },
                                )
                            attached_image_paths.add(absolute_path)
                        submitted = await self._request(
                            websocket,
                            "prompt.submit",
                            prompt_params,
                            buffered,
                            timeout_seconds=endpoint.timeout_seconds,
                        )
                        submit_status = str(
                            submitted.get("status") or ""
                        ).strip().lower()
                        if submit_status in {"steered", "redirected"}:
                            raise HermesProtocolError(
                                "runtime dashboard returned a busy prompt.submit disposition: "
                                f"{submit_status}",
                                rpc_method="prompt.submit",
                                rpc_data={"status": submit_status},
                            )
                        if submit_status not in {"streaming", "submitted", "queued"}:
                            raise HermesProtocolError(
                                f"unsupported prompt.submit status: {submit_status or 'missing'}",
                                rpc_method="prompt.submit",
                                rpc_data={"status": submit_status or None},
                            )
                        # A successful ACK proves that this exact Dashboard
                        # connection accepted the turn. Hermes 0.20.1 does not
                        # reliably emit message.start, so establish the main
                        # model boundary epoch here; a later message.start is
                        # an idempotent confirmation of the same epoch.
                        if (
                            connection.control_lease is not None
                            and submit_status != "queued"
                        ):
                            await connection.control_lease.observe_message_start()
                        if timing is not None:
                            timing.mark("prompt_ack")
                            _log_dashboard_timing(
                                timing,
                                stage="prompt_ack",
                                status="success",
                            )
                        if prompt_ack_sink is not None:
                            await prompt_ack_sink()
                        if timing is not None:
                            timing.set_metadata("prompt_submit_status", submit_status or "missing")
                        # `submitted` is the pre-0.19 ACK used by older runtime
                        # images and has no queue semantics, so preserve its
                        # immediate-open behavior.  A queued ACK only proves
                        # that Hermes accepted the next turn; real model output
                        # must open the event gate at message.start.
                        turn_gate_open = submit_status != "queued"
                        # Submit the prompt before yielding metadata. The platform persists
                        # run.started synchronously, so yielding it earlier needlessly kept
                        # the model idle behind database commits.
                        yield PlatformStreamEvent(
                            event="chat.run.started",
                            data={"run_id": live_session_id, "session_id": runtime_session_id},
                        )
                    else:
                        inflight = resumed.get("inflight")
                        if (
                            submit_status is None
                            and self._inflight_matches_message(
                                inflight, message, workspace=workspace
                            )
                        ):
                            # The submit ACK was lost with the transport, but
                            # canonical inflight state proves this exact user
                            # prompt owns the resumed turn.
                            submit_status = "streaming"
                            turn_gate_open = True
                        assistant = inflight.get("assistant") if isinstance(inflight, dict) else None
                        if isinstance(assistant, str):
                            suffix = self._completion_suffix(streamed_text, assistant)
                            if suffix:
                                streamed_text += suffix
                                yield PlatformStreamEvent(
                                    "chat.activity.delta",
                                    {
                                        "delta": suffix,
                                        "source": "recovered",
                                        "recovered": True,
                                        "interim": True,
                                    },
                                )
                        yield PlatformStreamEvent(
                            "chat.transport.resumed",
                            {"run_id": live_session_id, "attempt": reconnect_count},
                        )
                        recovered_final = self._completed_turn_text(
                            resumed.get("messages"), message, workspace=workspace
                        )
                        if recovered_final is not None and not self._resume_is_running(resumed) and not parked:
                            recovered_final, recovered_labels = progress_markers.strip_complete(recovered_final)
                            if recovered_labels:
                                latest_progress_label = recovered_labels[-1]
                            suffix = self._completion_suffix(streamed_text, recovered_final)
                            if suffix:
                                streamed_text += suffix
                                yield PlatformStreamEvent(
                                    "chat.message.delta",
                                    {"delta": suffix, "source": "agent", "recovered": True},
                                )
                            yield PlatformStreamEvent(
                                "chat.message.completed",
                                {"text": recovered_final, "status": "complete", "recovered": True},
                            )
                            yield PlatformStreamEvent("chat.run.completed", {"run_id": live_session_id})
                            if connection.hot_lease is not None:
                                connection.hot_lease.mark_clean_terminal()
                            yield PlatformStreamEvent("chat.done", {"run_id": live_session_id})
                            terminal = True

                    loop = asyncio.get_running_loop()
                    checkpoint_deadline = loop.time() + self.resume_poll_seconds
                    while not terminal:
                        checkpoint_due = loop.time() >= checkpoint_deadline
                        if buffered and not checkpoint_due:
                            frame = buffered.pop(0)
                        else:
                            try:
                                if checkpoint_due:
                                    raise asyncio.TimeoutError
                                frame = await asyncio.wait_for(
                                    self._receive_frame(websocket),
                                    timeout=max(
                                        0.001,
                                        checkpoint_deadline - loop.time(),
                                    ),
                                )
                            except asyncio.TimeoutError:
                                checkpoint_deadline = (
                                    loop.time() + self.resume_poll_seconds
                                )
                                # Dashboard events are process-local. If a Knative
                                # reconnect lands on a new revision, the original
                                # agent can finish against the shared state DB
                                # without the new socket seeing message.complete.
                                # Poll canonical session state so the platform can
                                # recover that terminal answer without resubmitting.
                                checkpoint_params: dict[str, Any] = {
                                    "session_id": runtime_session_id,
                                    "cols": 96,
                                }
                                if hermes_profile_name != "default":
                                    checkpoint_params["profile"] = hermes_profile_name
                                checkpoint = await self._request(
                                    websocket,
                                    "session.resume",
                                    checkpoint_params,
                                    buffered,
                                )
                                inflight = checkpoint.get("inflight")
                                assistant = inflight.get("assistant") if isinstance(inflight, dict) else None
                                if isinstance(assistant, str):
                                    suffix = self._completion_suffix(streamed_text, assistant)
                                    if suffix:
                                        streamed_text += suffix
                                        yield PlatformStreamEvent(
                                            "chat.activity.delta",
                                            {
                                                "delta": suffix,
                                                "source": "recovered",
                                                "recovered": True,
                                                "interim": True,
                                            },
                                        )
                                recovered_final = self._completed_turn_text(
                                    checkpoint.get("messages"), message, workspace=workspace
                                )
                                if (
                                    recovered_final is None
                                    and not parked
                                    and self._resume_allows_history_recovery(checkpoint)
                                ):
                                    try:
                                        history = await self._get_latest_session_messages(
                                            endpoint,
                                            runtime_session_id=runtime_session_id,
                                            hermes_profile_name=hermes_profile_name,
                                            initiating_user_message=message,
                                            workspace=workspace,
                                        )
                                    except (HermesProtocolError, httpx.HTTPError) as exc:
                                        logger.info(
                                            "dashboard terminal history recovery deferred "
                                            "workspace_id=%s session_id=%s error=%s",
                                            endpoint.runtime_workspace_id,
                                            runtime_session_id,
                                            exc,
                                        )
                                    else:
                                        recovered_final = self._completed_turn_text(
                                            history.get("messages"),
                                            message,
                                            workspace=workspace,
                                        )
                                if (
                                    recovered_final is not None
                                    and self._resume_allows_history_recovery(checkpoint)
                                    and not parked
                                ):
                                    recovered_final, recovered_labels = progress_markers.strip_complete(recovered_final)
                                    if recovered_labels:
                                        latest_progress_label = recovered_labels[-1]
                                    suffix = self._completion_suffix(streamed_text, recovered_final)
                                    if suffix:
                                        streamed_text += suffix
                                        yield PlatformStreamEvent(
                                            "chat.message.delta",
                                            {"delta": suffix, "source": "agent", "recovered": True},
                                        )
                                    yield PlatformStreamEvent(
                                        "chat.message.completed",
                                        {"text": recovered_final, "status": "complete", "recovered": True},
                                    )
                                    yield PlatformStreamEvent(
                                        "chat.run.completed", {"run_id": live_session_id}
                                    )
                                    if connection.hot_lease is not None:
                                        connection.hot_lease.mark_clean_terminal()
                                    yield PlatformStreamEvent("chat.done", {"run_id": live_session_id})
                                    terminal = True
                                continue
                        event = self._gateway_event(frame, live_session_id)
                        if event is None:
                            continue
                        event_type, payload = event

                        if not turn_gate_open:
                            if event_type == "message.start":
                                if connection.control_lease is not None:
                                    await connection.control_lease.observe_message_start()
                                turn_gate_open = True
                                streamed_text = ""
                                continue
                            continue

                        if event_type == "message.start":
                            if connection.control_lease is not None:
                                await connection.control_lease.observe_message_start()
                            streamed_text = ""
                            if parked:
                                parked = False
                                yield PlatformStreamEvent("chat.turn.resumed", {"run_id": live_session_id})
                            continue

                        if event_type.startswith("subagent."):
                            subagent_status = self._normalized_subagent_status(event_type, payload)
                            subagent_payload = {
                                **payload,
                                "type": event_type,
                                "status": subagent_status,
                            }
                            yield PlatformStreamEvent(
                                "chat.subagent.event",
                                subagent_payload,
                            )
                            if subagent_status == "failed":
                                yield PlatformStreamEvent(
                                    "chat.error",
                                    {
                                        "code": "subagent_failed",
                                        "message": "质量检查未完成，请重试",
                                    },
                                )
                                yield PlatformStreamEvent(
                                    "chat.done",
                                    {"run_id": live_session_id, "status": "failed"},
                                )
                                terminal = True
                            continue

                        if event_type == "reasoning.delta":
                            text = payload.get("text") or payload.get("delta")
                            if isinstance(text, str) and text:
                                reasoning_streamed = True
                                streamed_reasoning += text
                                yield PlatformStreamEvent("chat.reasoning.delta", {**payload, "delta": text})
                            continue

                        if event_type == "reasoning.available":
                            # Hermes 0.19 emits this as a full fallback snapshot.
                            # Its native clients ignore it after incremental
                            # reasoning has already arrived; appending it would
                            # duplicate (and can even echo) the final answer.
                            if reasoning_streamed:
                                continue
                            text = payload.get("text") or payload.get("delta")
                            if isinstance(text, str) and text:
                                reasoning_streamed = True
                                streamed_reasoning = text
                                yield PlatformStreamEvent("chat.reasoning.delta", {**payload, "delta": text})
                            continue

                        if event_type == "message.interim":
                            text = payload.get("text")
                            if (
                                isinstance(text, str)
                                and text
                                and payload.get("already_streamed") is not True
                            ):
                                visible, labels = progress_markers.feed(text)
                                if labels:
                                    latest_progress_label = labels[-1]
                                    yield PlatformStreamEvent(
                                        "chat.progress.delta",
                                        {
                                            "delta": latest_progress_label,
                                            "source": "agent",
                                            "state": "running",
                                        },
                                    )
                                if visible:
                                    streamed_text += visible
                                    yield PlatformStreamEvent(
                                        "chat.activity.delta",
                                        {**payload, "delta": visible, "source": "agent"},
                                    )
                            continue

                        if event_type == "message.delta":
                            text = payload.get("text")
                            if isinstance(text, str) and text:
                                visible, labels = progress_markers.feed(text)
                                if labels:
                                    latest_progress_label = labels[-1]
                                    yield PlatformStreamEvent(
                                        "chat.progress.delta",
                                        {
                                            "delta": latest_progress_label,
                                            "source": "agent",
                                            "state": "running",
                                        },
                                    )
                                if visible:
                                    streamed_text += visible
                                    yield PlatformStreamEvent(
                                        "chat.message.delta",
                                        {**payload, "delta": visible, "source": "agent"},
                                    )
                            continue

                        if event_type in {"tool.start", "tool.complete", "tool.error"}:
                            if connection.control_lease is not None:
                                await connection.control_lease.observe_tool_event(
                                    event_type,
                                    payload,
                                )
                            clarify_request_id = self._clarify_park_request_id(
                                payload
                            )
                            if clarify_request_id is not None:
                                # The platform plugin has already persisted the
                                # card in Backend. Stop this physical Hermes
                                # turn before the model can consume the marker
                                # as an ordinary tool error and keep running.
                                try:
                                    await self.stop_run(
                                        endpoint,
                                        runtime_provider_run_id=live_session_id,
                                    )
                                except Exception:
                                    logger.warning(
                                        "clarify_park_interrupt_failed session_id=%s",
                                        runtime_session_id,
                                        exc_info=True,
                                    )
                                yield PlatformStreamEvent(
                                    "chat.clarify.requested",
                                    {"request_id": clarify_request_id},
                                )
                                yield PlatformStreamEvent(
                                    "chat.done",
                                    {
                                        "run_id": live_session_id,
                                        "status": "parked_for_clarify",
                                    },
                                )
                                terminal = True
                                continue
                            yield PlatformStreamEvent(
                                "chat.tool.event",
                                self._tool_payload(event_type, payload, runtime_session_id),
                                internal=(
                                    extract_skill_manage_completion(payload)
                                    if event_type == "tool.complete"
                                    else None
                                ),
                            )
                            capability_error = capability_error_from_tool_payload(
                                payload,
                                run_id=str(runtime_run_id or live_session_id),
                            )
                            if capability_error is not None:
                                yield PlatformStreamEvent(
                                    "capability.error",
                                    capability_error.data.model_dump(mode="json"),
                                )
                            continue

                        if event_type == "approval.request":
                            pending: list[dict[str, Any]] = []
                            await self._request(
                                websocket,
                                "approval.respond",
                                {
                                    "session_id": live_session_id,
                                    "choice": "once",
                                    "all": False,
                                },
                                pending,
                            )
                            buffered[0:0] = pending
                            continue

                        if event_type == "message.complete":
                            terminal_payload = _public_terminal_payload(payload)
                            usage = terminal_payload.get("usage")
                            active_subagents = self._active_subagent_count(usage)
                            completion_status = str(
                                terminal_payload.get("status") or "complete"
                            ).lower()
                            final_text = (
                                terminal_payload.get("text")
                                or terminal_payload.get("rendered")
                            )
                            completed_payload = dict(terminal_payload)
                            if (
                                streamed_reasoning
                                and not isinstance(
                                    completed_payload.get("reasoning"),
                                    str,
                                )
                            ):
                                completed_payload["reasoning"] = streamed_reasoning
                            if completion_status == "complete" and isinstance(final_text, str):
                                final_text, labels = progress_markers.strip_complete(final_text)
                                completed_payload["text"] = final_text
                                if labels:
                                    latest_progress_label = labels[-1]
                                suffix = self._completion_suffix(streamed_text, final_text)
                                if suffix:
                                    streamed_text += suffix
                                    yield PlatformStreamEvent(
                                        "chat.message.delta",
                                        {"delta": suffix, "source": "agent"},
                                    )
                            if completion_status == "interrupted":
                                if (
                                    isinstance(final_text, str)
                                    and final_text.strip()
                                    and not _is_waiting_for_model_interrupt(final_text)
                                ):
                                    yield PlatformStreamEvent("chat.message.completed", completed_payload)
                                if isinstance(usage, dict):
                                    yield PlatformStreamEvent(
                                        "chat.usage",
                                        {"usage": self._usage_payload(usage)},
                                    )
                                yield PlatformStreamEvent(
                                    "chat.done",
                                    {"run_id": live_session_id, "status": "canceled"},
                                )
                                terminal = True
                                continue
                            if active_subagents > 0:
                                completed_payload["interim"] = True
                            yield PlatformStreamEvent("chat.message.completed", completed_payload)
                            if isinstance(usage, dict):
                                yield PlatformStreamEvent("chat.usage", {"usage": self._usage_payload(usage)})
                            if active_subagents > 0:
                                parked = True
                                if latest_progress_label is None:
                                    latest_progress_label = "正在处理任务"
                                    yield PlatformStreamEvent(
                                        "chat.progress.delta",
                                        {
                                            "delta": latest_progress_label,
                                            "source": "fallback",
                                            "state": "waiting_subagents",
                                        },
                                    )
                                yield PlatformStreamEvent(
                                    "chat.turn.parked",
                                    {"run_id": live_session_id, "active_subagents": active_subagents},
                                )
                                continue
                            if completion_status != "complete":
                                message_text = (
                                    final_text
                                    or terminal_payload.get("error")
                                    or "runtime dashboard turn failed"
                                )
                                yield PlatformStreamEvent(
                                    "chat.error",
                                    _public_chat_error_payload(message_text),
                                )
                                yield PlatformStreamEvent("chat.done", {"run_id": live_session_id, "status": "failed"})
                                terminal = True
                                continue
                            yield PlatformStreamEvent("chat.run.completed", {"run_id": live_session_id})
                            if connection.hot_lease is not None:
                                connection.hot_lease.mark_clean_terminal()
                            yield PlatformStreamEvent("chat.done", {"run_id": live_session_id})
                            terminal = True
                            continue

                        if event_type == "error":
                            message_text = payload.get("message") or payload.get("error") or "runtime dashboard stream failed"
                            yield PlatformStreamEvent(
                                "chat.error",
                                _public_chat_error_payload(message_text),
                            )
                            yield PlatformStreamEvent("chat.done", {"run_id": live_session_id})
                            terminal = True
            except asyncio.CancelledError:
                raise
            except DashboardPromptSubmitTimeout:
                raise
            except Exception as exc:
                if terminal or reconnect_count >= self.reconnect_attempts or not self._is_recoverable_disconnect(exc):
                    if timing is not None:
                        _log_dashboard_timing(
                            timing,
                            stage="failed",
                            status="error",
                            error_type=exc.__class__.__name__,
                        )
                    raise
                timeout_failure = (
                    isinstance(exc, (TimeoutError, asyncio.TimeoutError))
                    or "timed out" in str(exc).lower()
                )
                if (
                    not prompt_attempted
                    and pre_prompt_timeout_seconds is not None
                    and not use_hot_this_attempt
                    and timeout_failure
                ):
                    raise
                # A recoverable failure while acquiring/preparing a HotChannel
                # must not restart the same hot preparation on the next loop.
                # Prompt recovery remains governed solely by prompt_attempted.
                hot_attempted = True
                reconnect_count += 1
                yield PlatformStreamEvent(
                    "chat.transport.reconnecting",
                    {
                        "attempt": reconnect_count,
                        "message": "Dashboard connection interrupted",
                        # A lost submit ACK can still mean Hermes accepted and
                        # executed the prompt, so an attempted submit is the
                        # conservative durable-reconcile boundary.
                        "prompt_submitted": prompt_attempted,
                    },
                )
                if self.reconnect_backoff_seconds:
                    await asyncio.sleep(
                        min(10.0, self.reconnect_backoff_seconds * (2 ** (reconnect_count - 1)))
                    )

    async def _prepare_hot_peer(
        self,
        peer: DashboardRpcPeer,
        *,
        runtime_session_id: str,
        hermes_profile_name: str,
        runtime_agent_profile_id: int | None,
        expected_model: str | None,
        timeout_seconds: float,
        eager_build: bool,
        expected_provider: str | None = None,
        route_prepare_proof: DashboardSessionRouteProof | None = None,
    ) -> DashboardPreparedPeer:
        websocket = DashboardRpcPeerSocket(peer)
        buffered: list[dict[str, Any]] = []
        try:
            workspace = self._session_workspace_path(
                runtime_session_id,
                hermes_profile_name=hermes_profile_name,
                runtime_agent_profile_id=runtime_agent_profile_id,
            )
            if workspace:
                await self._ensure_session_workspace(
                    websocket,
                    buffered,
                    runtime_session_id=runtime_session_id,
                    hermes_profile_name=hermes_profile_name,
                    runtime_agent_profile_id=runtime_agent_profile_id,
                    timeout_seconds=timeout_seconds,
                )
            params: dict[str, Any] = {
                "session_id": runtime_session_id,
                "cols": 96,
                "omit_messages": True,
            }
            if hermes_profile_name != "default":
                params["profile"] = hermes_profile_name
            if eager_build:
                params["eager_build"] = True
            resumed = await self._request(
                websocket,
                "session.resume",
                params,
                buffered,
                timeout_seconds=timeout_seconds,
            )
            self._validate_omitted_resume(resumed)
            live_session_id = str(resumed.get("session_id") or "")
            if not live_session_id:
                raise HermesProtocolError("runtime dashboard did not return a live session id")
            force_platform_provider = (
                hermes_profile_name != "default" or bool(expected_provider)
            )
            if force_platform_provider and not str(expected_model or "").strip():
                raise HermesProtocolError(
                    "runtime dashboard expert model is unavailable"
                )
            if str(expected_model or "").strip():
                await self._ensure_session_model(
                    websocket,
                    buffered,
                    runtime_session_id=runtime_session_id,
                    resumed=resumed,
                    expected_model=expected_model,
                    force_platform_provider=force_platform_provider,
                    expected_provider=expected_provider,
                    expected_profile=hermes_profile_name,
                    route_prepare_proof=route_prepare_proof,
                )
            info = resumed.get("info")
            if workspace:
                # A previously imported session can already report the target
                # cwd even though an older backend never created it.  Always
                # run the idempotent mkdir before using the hot peer so those
                # legacy sessions are repaired as well.
                workspace = await self._prepare_session_workspace(
                    websocket,
                    buffered,
                    live_session_id=live_session_id,
                    runtime_session_id=runtime_session_id,
                    hermes_profile_name=hermes_profile_name,
                    runtime_agent_profile_id=runtime_agent_profile_id,
                    ensure_exists=False,
                    timeout_seconds=timeout_seconds,
                )
            return DashboardPreparedPeer(
                live_session_id=live_session_id,
                workspace=workspace,
                model=str(expected_model or "").strip() or None,
                provider=(
                    expected_provider or _PLATFORM_MODEL_PROVIDER
                    if force_platform_provider
                    else None
                ),
                ready=(
                    not isinstance(info, dict)
                    or info.get("lazy") is not True
                ),
            )
        finally:
            await websocket.close()

    @staticmethod
    async def _wait_hot_peer_ready(
        peer: DashboardRpcPeer,
        live_session_id: str,
        *,
        timeout_seconds: float,
    ) -> None:
        def matches(frame: dict[str, Any]) -> bool:
            params = frame.get("params")
            if (
                not isinstance(params, dict)
                or params.get("session_id") != live_session_id
            ):
                return False
            if params.get("type") == "error":
                return True
            payload = params.get("payload")
            return (
                params.get("type") == "session.info"
                and isinstance(payload, dict)
                and payload.get("lazy") is not True
            )

        frame = await peer.wait_for_notification(
            matches,
            timeout_seconds=timeout_seconds,
        )
        params = frame.get("params") or {}
        if params.get("type") == "error":
            payload = params.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            raise HermesProtocolError(
                str(
                    payload.get("message")
                    or payload.get("error")
                    or "runtime dashboard agent initialization failed"
                )
            )

    async def _dashboard_json_request(
        self,
        endpoint: RuntimeEndpoint,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = await self._dashboard_json_value_request(
            endpoint,
            method,
            path,
            json_body=json_body,
        )
        if not isinstance(payload, dict):
            raise HermesProtocolError(
                f"{path} returned non-object payload",
                path=path,
            )
        return payload

    async def _dashboard_json_value_request(
        self,
        endpoint: RuntimeEndpoint,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        response = await _dashboard_http_sessions.request(
            endpoint,
            method,
            path,
            json=json_body,
        )
        status_code = int(response.status_code)
        if status_code < 200 or status_code >= 300:
            raise HermesProtocolError(
                f"{path} failed with status {status_code}",
                status_code=status_code,
                path=path,
            )
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise HermesProtocolError(
                f"{path} returned invalid JSON payload",
                path=path,
            ) from exc
        return payload

    async def _ensure_session_model(
        self,
        websocket: DashboardWebSocket,
        buffered: list[dict[str, Any]],
        *,
        runtime_session_id: str,
        resumed: dict[str, Any],
        expected_model: str | None,
        force_platform_provider: bool,
        expected_provider: str | None = None,
        expected_profile: str = "default",
        route_prepare_proof: DashboardSessionRouteProof | None = None,
    ) -> None:
        expected = str(expected_model or "").strip()
        if not expected:
            return
        info = resumed.get("info")
        current = str(info.get("model") or "").strip() if isinstance(info, dict) else ""
        reported_profile = str(
            resumed.get("profile")
            or (info.get("profile") if isinstance(info, dict) else "")
            or ""
        ).strip()
        profile = str(expected_profile or "default").strip() or "default"
        if reported_profile and reported_profile != profile:
            raise HermesProtocolError("runtime dashboard session route mismatch")
        if current != expected:
            raise HermesProtocolError("runtime dashboard session route mismatch")
        expected_route_provider = (
            expected_provider or _PLATFORM_MODEL_PROVIDER
        )
        current_provider = (
            str(info.get("provider") or "").strip()
            if isinstance(info, dict)
            else ""
        )
        if not force_platform_provider or current_provider == expected_route_provider:
            return
        if current_provider == "custom" and self._matches_route_prepare_proof(
            route_prepare_proof,
            runtime_session_id=runtime_session_id,
            expected_model=expected,
            expected_provider=expected_route_provider,
            expected_profile=profile,
        ):
            return
        raise HermesProtocolError("runtime dashboard session route mismatch")

    @staticmethod
    def _matches_route_prepare_proof(
        proof: DashboardSessionRouteProof | None,
        *,
        runtime_session_id: str,
        expected_model: str,
        expected_provider: str,
        expected_profile: str,
    ) -> bool:
        return (
            isinstance(proof, DashboardSessionRouteProof)
            and proof._authority is _ROUTE_PREPARE_PROOF_AUTHORITY
            and proof.runtime_session_id == runtime_session_id
            and proof.hermes_profile_name == expected_profile
            and proof.expected_model == expected_model
            and proof.expected_provider == expected_provider
        )

    @asynccontextmanager
    async def _open_chat_connection(
        self,
        endpoint: RuntimeEndpoint,
        *,
        runtime_session_id: str,
        runtime_run_id: str | None,
        hermes_profile_name: str,
        runtime_agent_profile_id: int | None,
        expected_model: str | None,
        expected_provider: str | None,
        hot_channel_identity: DashboardHotChannelIdentity | None,
        use_hot: bool,
        route_prepare_proof: DashboardSessionRouteProof | None = None,
        timing: Any | None = None,
    ) -> AsyncIterator[_ChatConnection]:
        if (
            use_hot
            and hot_channel_identity is not None
            and runtime_run_id is not None
            and self.connector is not None
        ):
            async def prepare(peer: DashboardRpcPeer) -> DashboardPreparedPeer:
                return await self._prepare_hot_peer(
                    peer,
                    runtime_session_id=runtime_session_id,
                    hermes_profile_name=hermes_profile_name,
                    runtime_agent_profile_id=runtime_agent_profile_id,
                    expected_model=(
                        expected_model
                        or _hot_channel_expected_model(hot_channel_identity)
                    ),
                    timeout_seconds=endpoint.timeout_seconds,
                    eager_build=False,
                    expected_provider=expected_provider,
                    route_prepare_proof=route_prepare_proof,
                )

            async def wait_ready(
                peer: DashboardRpcPeer,
                live_session_id: str,
            ) -> None:
                await self._wait_hot_peer_ready(
                    peer,
                    live_session_id,
                    timeout_seconds=endpoint.timeout_seconds,
                )

            try:
                lease = await self.hot_channels.acquire_run(
                    hot_channel_identity,
                    endpoint,
                    self.connector,
                    prepare,
                    wait_ready=wait_ready,
                    runtime_run_id=runtime_run_id,
                    on_result=(
                        lambda outcome: timing.set_metadata(
                            "hot_channel",
                            outcome,
                        )
                        if timing is not None
                        else None
                    ),
                )
            except DashboardHotCapacityUnavailable:
                lease = None
            if lease is None:
                if timing is not None:
                    timing.set_metadata(
                        "hot_channel",
                        "capacity_cold_fallback",
                    )
            else:
                if timing is not None:
                    timing.mark("hot_channel_acquired")
                websocket = DashboardRpcPeerSocket(lease.peer, lease=lease)
                control_lease: ActiveRunControlPeerLease | None = None
                terminal_release = False
                try:
                    control_lease = await self.control_peers.register(
                        runtime_run_id=runtime_run_id,
                        stored_session_id=runtime_session_id,
                        peer=lease.peer,
                    )
                    yield _ChatConnection(
                        websocket=websocket,
                        hot_lease=lease,
                        control_lease=control_lease,
                        live_session_id=lease.live_session_id,
                        workspace=lease.workspace,
                        model=lease.model,
                        provider=lease.provider,
                    )
                    terminal_release = True
                finally:
                    if control_lease is not None:
                        await control_lease.close()
                    await websocket.close()
                    await lease.release(
                        terminal=(terminal_release or lease.clean_terminal)
                    )
                return
        assert self.connector is not None
        if timing is not None:
            timing.set_metadata("hot_channel", "cold")
        async with self.hot_channels.cold_run(endpoint.coordination_scope):
            async with self.connector(endpoint) as raw_websocket:
                if timing is not None:
                    timing.mark("hot_channel_acquired")
                if runtime_run_id is None:
                    yield _ChatConnection(websocket=raw_websocket)
                    return
                peer = DashboardRpcPeer(raw_websocket)
                websocket = DashboardRpcPeerSocket(
                    peer,
                    start_immediately=False,
                )
                control_lease: ActiveRunControlPeerLease | None = None
                try:
                    control_lease = await self.control_peers.register(
                        runtime_run_id=runtime_run_id,
                        stored_session_id=runtime_session_id,
                        peer=peer,
                    )
                    yield _ChatConnection(
                        websocket=websocket,
                        control_lease=control_lease,
                    )
                finally:
                    if control_lease is not None:
                        await control_lease.close()
                    await websocket.close()
                    await peer.close()

    async def stop_run(
        self,
        endpoint: RuntimeEndpoint,
        *,
        runtime_provider_run_id: str,
    ) -> dict[str, Any]:
        if self.connector is None:
            raise HermesProtocolError("runtime dashboard connector is unavailable")
        async with self.connector(endpoint) as websocket:
            buffered: list[dict[str, Any]] = []
            return await self._request(
                websocket,
                "session.interrupt",
                {"session_id": runtime_provider_run_id},
                buffered,
                timeout_seconds=endpoint.timeout_seconds,
            )

    async def interrupt_and_settle_redirect(
        self,
        endpoint: RuntimeEndpoint,
        *,
        runtime_session_id: str,
        hermes_profile_name: str = "default",
        timeout_seconds: float = 30.0,
    ) -> DashboardInterruptSettleResult:
        """Recover an unowned Session without submitting or resending a prompt."""

        if self.connector is None:
            raise HermesProtocolError("runtime dashboard connector is unavailable")
        deadline = asyncio.get_running_loop().time() + max(0.1, timeout_seconds)
        resume_params: dict[str, Any] = {
            "session_id": runtime_session_id,
            "omit_messages": True,
            "cols": 96,
        }
        if hermes_profile_name != "default":
            resume_params["profile"] = hermes_profile_name
        async with self.connector(endpoint) as websocket:
            buffered: list[dict[str, Any]] = []
            checkpoint = await self._request(
                websocket,
                "session.resume",
                resume_params,
                buffered,
                timeout_seconds=self._redirect_settle_remaining(deadline),
            )
            if ActiveRunControlPeerRegistry.redirect_checkpoint_is_settled(
                checkpoint
            ):
                return DashboardInterruptSettleResult(
                    interrupt_sent=False,
                    interrupt_result=None,
                    checkpoint=checkpoint,
                )

            live_session_id = str(
                checkpoint.get("session_id") or runtime_session_id
            )
            interrupt_result = await self._request(
                websocket,
                "session.interrupt",
                {"session_id": live_session_id},
                buffered,
                timeout_seconds=self._redirect_settle_remaining(deadline),
            )
            while True:
                checkpoint = await self._request(
                    websocket,
                    "session.resume",
                    resume_params,
                    buffered,
                    timeout_seconds=self._redirect_settle_remaining(deadline),
                )
                if ActiveRunControlPeerRegistry.redirect_checkpoint_is_settled(
                    checkpoint
                ):
                    return DashboardInterruptSettleResult(
                        interrupt_sent=True,
                        interrupt_result=interrupt_result,
                        checkpoint=checkpoint,
                    )
                await asyncio.sleep(
                    min(0.1, self._redirect_settle_remaining(deadline))
                )

    @staticmethod
    def _redirect_settle_remaining(deadline: float) -> float:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise HermesProtocolError(
                "runtime Dashboard redirect Session did not settle",
                rpc_method="session.resume",
            )
        return remaining

    async def list_session_artifacts(
        self,
        endpoint: RuntimeEndpoint,
        *,
        runtime_session_id: str,
        runtime_agent_profile_id: int | None = None,
        hermes_profile_name: str = "default",
    ) -> list[ManagedRuntimeArtifact]:
        if not self.session_workspace_root:
            return []
        session_dir = self._session_workspace_path(
            runtime_session_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
            hermes_profile_name=hermes_profile_name,
        )
        if session_dir is None:
            return []
        async with authenticated_dashboard_client(endpoint) as (client, base_url, headers):
            response = await client.get(
                f"{base_url}/api/files",
                headers=headers,
                params={"path": session_dir},
            )
            if response.status_code == 404:
                return []
            if response.status_code < 200 or response.status_code >= 300:
                raise HermesProtocolError(
                    f"runtime artifact listing failed with status {response.status_code}"
                )
            payload = response.json()
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise HermesProtocolError("runtime artifact listing returned invalid entries")
        artifacts: list[ManagedRuntimeArtifact] = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("is_directory"):
                continue
            raw_path = str(entry.get("path") or f"{session_dir}/{entry.get('name') or ''}")
            relative_path = self._workspace_relative_path(raw_path)
            if relative_path is None or self._is_process_artifact(relative_path):
                continue
            try:
                size = max(0, int(entry.get("size") or 0))
            except (TypeError, ValueError):
                size = 0
            artifacts.append(
                ManagedRuntimeArtifact(
                    relative_path=relative_path,
                    mime_type=str(entry.get("mime_type")) if entry.get("mime_type") else None,
                    size_bytes=size,
                    mtime=_managed_entry_mtime(entry),
                )
            )
        return artifacts

    async def stream_managed_artifact(
        self,
        endpoint: RuntimeEndpoint,
        *,
        relative_path: str,
    ):
        absolute_path = self._managed_absolute_path(relative_path)
        async with authenticated_dashboard_client(endpoint) as (client, base_url, headers):
            async with client.stream(
                "GET",
                f"{base_url}/api/files/download",
                headers=headers,
                params={"path": absolute_path},
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise HermesProtocolError(
                        f"runtime artifact download failed with status {response.status_code}"
                    )
                async for chunk in response.aiter_raw():
                    if chunk:
                        yield chunk

    async def upload_managed_artifact(
        self,
        endpoint: RuntimeEndpoint,
        *,
        relative_path: str,
        content: BinaryIO,
        filename: str,
        mime_type: str | None,
    ) -> ManagedRuntimeArtifact:
        absolute_path = self._managed_absolute_path(relative_path)
        async with authenticated_dashboard_client(endpoint) as (client, base_url, headers):
            response = await client.post(
                f"{base_url}/api/files/upload-stream",
                headers=headers,
                data={"path": absolute_path, "overwrite": "false"},
                files={
                    "file": (
                        filename,
                        content,
                        mime_type or "application/octet-stream",
                    )
                },
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise HermesProtocolError(
                f"runtime artifact upload failed with status {response.status_code}"
            )
        payload = response.json()
        entry = payload.get("entry") if isinstance(payload, dict) else None
        if not isinstance(entry, dict):
            raise HermesProtocolError("runtime artifact upload returned an invalid entry")
        return ManagedRuntimeArtifact(
            relative_path=relative_path,
            mime_type=str(entry.get("mime_type")) if entry.get("mime_type") else mime_type,
            size_bytes=max(0, int(entry.get("size") or 0)),
            mtime=_managed_entry_mtime(entry),
        )

    async def rename_managed_artifact(
        self,
        endpoint: RuntimeEndpoint,
        *,
        source_relative_path: str,
        target_relative_path: str,
    ) -> None:
        source = self._managed_absolute_path(source_relative_path)
        target = self._managed_absolute_path(target_relative_path)
        if self.connector is None:
            raise HermesProtocolError("runtime dashboard connector is unavailable")
        async with self.connector(endpoint) as websocket:
            buffered: list[dict[str, Any]] = []
            result = await self._request(
                websocket,
                "shell.exec",
                {
                    "command": (
                        f"if [ -e {shlex.quote(target)} ]; then echo 'target exists' >&2; exit 1; fi; "
                        f"mkdir -p -- {shlex.quote(str(PurePosixPath(target).parent))} && "
                        f"mv -- {shlex.quote(source)} {shlex.quote(target)}"
                    )
                },
                buffered,
            )
        if int(result.get("code") or 0) != 0:
            detail = result.get("stderr") or result.get("stdout") or "rename failed"
            raise HermesProtocolError(f"runtime artifact rename failed: {detail}")

    async def delete_managed_artifact(
        self,
        endpoint: RuntimeEndpoint,
        *,
        relative_path: str,
    ) -> None:
        absolute_path = self._managed_absolute_path(relative_path)
        async with authenticated_dashboard_client(endpoint) as (client, base_url, headers):
            response = await client.request(
                "DELETE",
                f"{base_url}/api/files",
                headers=headers,
                json={"path": absolute_path, "recursive": False},
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise HermesProtocolError(
                f"runtime artifact delete failed with status {response.status_code}"
            )

    def _workspace_relative_path(self, absolute_path: str) -> str | None:
        if not self.session_workspace_root:
            return None
        root = PurePosixPath(self.session_workspace_root)
        path = PurePosixPath(absolute_path)
        try:
            relative = path.relative_to(root)
        except ValueError:
            return None
        return PurePosixPath("workspace", relative).as_posix()

    @staticmethod
    def _is_process_artifact(relative_path: str) -> bool:
        parts = PurePosixPath(relative_path).parts
        return any(
            part in {".work", ".venv", "__pycache__"}
            or part.startswith((".", "~"))
            for part in parts
        )

    def _managed_absolute_path(self, relative_path: str) -> str:
        if not self.session_workspace_root:
            raise HermesProtocolError("runtime artifact workspace is unavailable")
        path = PurePosixPath(relative_path)
        if path.is_absolute() or not path.parts or path.parts[0] != "workspace" or ".." in path.parts:
            raise HermesProtocolError("runtime artifact path is invalid")
        return str(PurePosixPath(self.session_workspace_root, *path.parts[1:]))

    def _session_workspace_path(
        self,
        runtime_session_id: str,
        *,
        hermes_profile_name: str = "default",
        runtime_agent_profile_id: int | None = None,
    ) -> str | None:
        if (
            hermes_profile_name != "default"
            and runtime_agent_profile_id is not None
            and self.session_workspace_root
        ):
            return dashboard_session_workspace_path(
                self.session_workspace_root,
                runtime_session_id,
                runtime_agent_profile_id=runtime_agent_profile_id,
                hermes_profile_name=hermes_profile_name,
            )
        return dashboard_session_workspace_path(
            self.session_workspace_root,
            runtime_session_id,
        )

    async def _prepare_session_workspace(
        self,
        websocket: DashboardWebSocket,
        buffered: list[dict[str, Any]],
        *,
        live_session_id: str,
        runtime_session_id: str,
        hermes_profile_name: str = "default",
        runtime_agent_profile_id: int | None = None,
        ensure_exists: bool = True,
        timeout_seconds: float | None = None,
    ) -> str:
        workspace = self._session_workspace_path(
            runtime_session_id,
            hermes_profile_name=hermes_profile_name,
            runtime_agent_profile_id=runtime_agent_profile_id,
        )
        if workspace is None:
            raise HermesProtocolError("runtime session workspace is unavailable")
        if ensure_exists:
            await self._ensure_session_workspace(
                websocket,
                buffered,
                runtime_session_id=runtime_session_id,
                hermes_profile_name=hermes_profile_name,
                runtime_agent_profile_id=runtime_agent_profile_id,
                timeout_seconds=timeout_seconds,
            )
        await self._request(
            websocket,
            "session.cwd.set",
            {"session_id": live_session_id, "cwd": workspace},
            buffered,
            timeout_seconds=timeout_seconds,
        )
        return workspace

    async def _ensure_session_workspace(
        self,
        websocket: DashboardWebSocket,
        buffered: list[dict[str, Any]],
        *,
        runtime_session_id: str,
        hermes_profile_name: str = "default",
        runtime_agent_profile_id: int | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        """Create the managed cwd before Dashboard initializes its Agent."""
        workspace = self._session_workspace_path(
            runtime_session_id,
            hermes_profile_name=hermes_profile_name,
            runtime_agent_profile_id=runtime_agent_profile_id,
        )
        if workspace is None:
            raise HermesProtocolError("runtime session workspace is unavailable")
        process_workspace = str(PurePosixPath(workspace, ".work"))
        shared_workspace = (
            str(PurePosixPath(workspace).parent.parent / "shared")
            if hermes_profile_name != "default"
            and runtime_agent_profile_id is not None
            else None
        )
        mkdir_targets = [workspace, process_workspace]
        if shared_workspace is not None:
            mkdir_targets.append(shared_workspace)
        probe_targets = [workspace, process_workspace]
        created = await self._request(
            websocket,
            "shell.exec",
            {
                "command": (
                    "mkdir -p -m 2770 -- "
                    + " ".join(shlex.quote(target) for target in mkdir_targets)
                    + " && "
                    + shlex.quote(_HERMES_PYTHON)
                    + " -m platform_supervisor.workspace_probe "
                    + " ".join(
                        "--directory " + shlex.quote(target)
                        for target in probe_targets
                    )
                )
            },
            buffered,
            timeout_seconds=timeout_seconds,
        )
        if int(created.get("code") or 0) != 0:
            detail = created.get("stderr") or created.get("stdout") or "mkdir failed"
            raise HermesProtocolError(f"runtime session workspace creation failed: {detail}")
        return workspace

    @classmethod
    async def _request(
        cls,
        websocket: DashboardWebSocket,
        method: str,
        params: dict[str, Any],
        buffered_events: list[dict[str, Any]],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        request_id = f"platform-{uuid4().hex}"
        loop = asyncio.get_running_loop()
        deadline = (
            loop.time() + max(0.1, float(timeout_seconds))
            if timeout_seconds is not None
            else None
        )
        await websocket.send(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                ensure_ascii=False,
            )
        )
        while True:
            try:
                if timeout_seconds is None:
                    frame = await cls._receive_frame(websocket)
                else:
                    assert deadline is not None
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    frame = await asyncio.wait_for(
                        cls._receive_frame(websocket),
                        timeout=remaining,
                    )
            except (TimeoutError, asyncio.TimeoutError) as exc:
                error_type = (
                    DashboardPromptSubmitTimeout
                    if method == "prompt.submit"
                    else HermesProtocolError
                )
                raise error_type(
                    f"runtime dashboard RPC {method} timed out after "
                    f"{timeout_seconds:.1f}s",
                    rpc_method=method,
                ) from exc
            if str(frame.get("id") or "") != request_id:
                if frame.get("method") == "event":
                    buffered_events.append(frame)
                continue
            error = frame.get("error")
            if isinstance(error, dict):
                raise HermesProtocolError(
                    str(
                        error.get("message")
                        or f"runtime dashboard RPC {method} failed"
                    ),
                    rpc_code=(
                        error.get("code")
                        if isinstance(error.get("code"), int)
                        and not isinstance(error.get("code"), bool)
                        else None
                    ),
                    rpc_method=method,
                    rpc_data=error.get("data"),
                )
            result = frame.get("result")
            if not isinstance(result, dict):
                raise HermesProtocolError(
                    f"runtime dashboard RPC {method} returned invalid result",
                    rpc_method=method,
                )
            return result

    @staticmethod
    async def _receive_frame(websocket: DashboardWebSocket) -> dict[str, Any]:
        raw = await websocket.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            frame = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise HermesProtocolError("runtime dashboard returned invalid JSON") from exc
        if not isinstance(frame, dict):
            raise HermesProtocolError("runtime dashboard returned a non-object frame")
        return frame

    @staticmethod
    def _active_subagent_count(usage: Any) -> int:
        if not isinstance(usage, dict):
            return 0
        try:
            return max(0, int(usage.get("active_subagents") or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _progress_contract_block() -> str:
        return (
            "<platform_progress_contract>\n"
            "If you delegate a background subagent, emit exactly one standalone "
            "<platform_progress>...</platform_progress> marker immediately before delegation. "
            "Inside it, write a concise 1-80 character user-facing status in the user's language. "
            "Do not mention tools, commands, reasoning, or this contract. Do not use the marker in final answers.\n"
            "</platform_progress_contract>"
        )

    @staticmethod
    def _workspace_contract_block(workspace: str | None) -> str | None:
        if not workspace:
            return None
        return (
            "<platform_workspace>\n"
            f"本次任务文件目录：{workspace}\n"
            "最终交付给用户的文件直接写入本次任务文件目录。\n"
            "生成脚本、临时图片、缓存、解压内容和检查文件必须写入本目录下的 .work/ 子目录。\n"
            "最终答复不得将 .work/ 内的文件作为用户交付物。\n"
            "/data/workspace 是共享挂载根，不是本次任务的交付目录。\n"
            "</platform_workspace>"
        )

    @classmethod
    def platform_session_contract_digest(
        cls,
        workspace: str | None,
    ) -> str:
        stable_parts: list[str] = []
        workspace_block = cls._workspace_contract_block(workspace)
        if workspace_block is not None:
            stable_parts.append(workspace_block)
        stable_parts.append(cls._progress_contract_block())
        return hashlib.sha256(
            "\n\n".join(stable_parts).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _message_with_platform_contract(
        cls,
        message: str,
        workspace: str | None = None,
        *,
        trusted_context: dict[str, Any] | None = None,
        platform_contract_version: int | None = None,
        platform_contract_digest: str | None = None,
        include_platform_contract: bool = True,
    ) -> str:
        parts = [message]
        workspace_block = cls._workspace_contract_block(workspace)
        if platform_contract_version is None and workspace_block is not None:
            parts.append(workspace_block)
        if trusted_context:
            parts.append(
                "<platform_trusted_user_context>\n"
                + json.dumps(
                    trusted_context,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n</platform_trusted_user_context>"
            )
        if platform_contract_version is None:
            parts.append(cls._progress_contract_block())
        elif platform_contract_version == 2 and include_platform_contract:
            digest = (
                str(platform_contract_digest or "").strip().lower()
                or cls.platform_session_contract_digest(workspace)
            )
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("invalid platform session contract digest")
            contract_parts: list[str] = []
            if workspace_block is not None:
                contract_parts.append(workspace_block)
            contract_parts.append(cls._progress_contract_block())
            parts.append(
                '<platform_session_contract version="2" '
                f'digest="{digest}">\n'
                + "\n\n".join(contract_parts)
                + "\n</platform_session_contract>"
            )
        elif platform_contract_version != 2:
            raise ValueError(
                f"unsupported platform session contract version: "
                f"{platform_contract_version}"
            )
        return "\n\n".join(parts)

    @classmethod
    def _message_with_progress_contract(cls, message: str) -> str:
        return cls._message_with_platform_contract(message)

    @staticmethod
    def _subagent_failure_message(payload: dict[str, Any]) -> str | None:
        for key in ("error", "message", "summary", "preview"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None

    @staticmethod
    def _normalized_subagent_status(event_type: str, payload: dict[str, Any]) -> str:
        raw_status = str(payload.get("status") or "").strip().lower()
        if event_type in {"subagent.failed", "subagent.error"} or raw_status in {
            "failed",
            "error",
            "timeout",
            "failure",
        }:
            return "failed"
        if event_type in {"subagent.interrupted", "subagent.canceled"} or raw_status in {
            "interrupted",
            "canceled",
            "cancelled",
        }:
            return "interrupted"
        if raw_status in {"queued", "running", "completed"}:
            return raw_status
        return "completed" if event_type == "subagent.complete" else "running"

    @staticmethod
    def _is_recoverable_disconnect(exc: Exception) -> bool:
        if isinstance(exc, HermesProtocolError) and exc.rpc_code is not None:
            return False
        if isinstance(
            exc,
            (
                ConnectionError,
                TimeoutError,
                OSError,
                DashboardPeerDisconnected,
                DashboardResumeRequired,
            ),
        ):
            return True
        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "no close frame",
                "connection failed",
                "connection closed",
                "connection reset",
                "keepalive ping timeout",
                "timed out",
                "status 502",
                "status 503",
                "status 504",
            )
        )

    @staticmethod
    def _resume_is_running(resumed: dict[str, Any]) -> bool:
        info = resumed.get("info")
        inflight = resumed.get("inflight")
        return bool(
            resumed.get("running")
            or str(resumed.get("status") or "").lower() in {"working", "waiting", "streaming"}
            or (
                isinstance(info, dict)
                and (
                    info.get("running")
                    or str(info.get("status") or "").lower() in {"working", "waiting", "streaming"}
                )
            )
            or (isinstance(inflight, dict) and inflight.get("streaming"))
        )

    @classmethod
    def _resume_allows_history_recovery(cls, resumed: dict[str, Any]) -> bool:
        info = resumed.get("info")
        status = str(resumed.get("status") or "").strip().lower()
        if not status and isinstance(info, dict):
            status = str(info.get("status") or "").strip().lower()
        usage = resumed.get("usage")
        if not isinstance(usage, dict) and isinstance(info, dict):
            usage = info.get("usage")
        return bool(
            status == "idle"
            and not cls._resume_is_running(resumed)
            and not resumed.get("inflight")
            and cls._active_subagent_count(usage) == 0
        )

    @classmethod
    def _completed_turn_text(
        cls, messages: Any, user_message: str, *, workspace: str | None = None
    ) -> str | None:
        if not isinstance(messages, list):
            return None
        expected_user_messages = {
            user_message.strip(),
            cls._message_with_progress_contract(user_message).strip(),
            cls._message_with_platform_contract(user_message, workspace).strip(),
        }
        matched_user = False
        assistant: str | None = None
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").lower()
            text = item.get("text", item.get("content"))
            if role == "user":
                matched_user = isinstance(text, str) and text.strip() in expected_user_messages
                assistant = None
            elif matched_user and role == "assistant" and isinstance(text, str):
                assistant = text
        return assistant

    @classmethod
    def _inflight_matches_message(
        cls, inflight: Any, user_message: str, *, workspace: str | None = None
    ) -> bool:
        if not isinstance(inflight, dict):
            return False
        inflight_user = inflight.get("user")
        if not isinstance(inflight_user, str):
            return False
        return inflight_user.strip() in {
            user_message.strip(),
            cls._message_with_progress_contract(user_message).strip(),
            cls._message_with_platform_contract(user_message, workspace).strip(),
        }

    @staticmethod
    def _gateway_event(
        frame: dict[str, Any], live_session_id: str
    ) -> tuple[str, dict[str, Any]] | None:
        if frame.get("method") != "event":
            return None
        params = frame.get("params")
        if not isinstance(params, dict):
            return None
        session_id = params.get("session_id")
        if session_id and str(session_id) != live_session_id:
            return None
        event_type = params.get("type")
        payload = params.get("payload")
        if not isinstance(event_type, str) or not isinstance(payload, dict):
            return None
        return event_type, payload

    @staticmethod
    def _completion_suffix(streamed_text: str, final_text: str) -> str:
        if not final_text:
            return ""
        if not streamed_text.strip():
            return final_text
        if final_text == streamed_text or final_text.strip() == streamed_text.strip():
            return ""
        return final_text[len(streamed_text) :] if final_text.startswith(streamed_text) else ""

    @staticmethod
    def _tool_payload(event_type: str, payload: dict[str, Any], session_id: str) -> dict[str, Any]:
        name = str(payload.get("name") or "tool")
        call_id = payload.get("tool_id") or payload.get("toolCallId")
        result = payload.get("result")
        result_error = result.get("error") if isinstance(result, dict) else None
        failed = bool(
            payload.get("error")
            or result_error
            or str(payload.get("status") or "").lower() in {"error", "failed"}
        )
        args: object = payload.get("args") if payload.get("args") is not None else payload.get("args_text")
        public_result: object = result if result is not None else payload.get("error")
        preview: object = payload.get("preview")
        if name == "skill_manage":
            # ``create.content`` can be an entire user Skill.  The public run
            # event is not an audit store, so retain only operation metadata.
            args = public_skill_manage_args(payload.get("args"))
            public_result = public_skill_manage_result(result)
            preview = None
        return build_public_tool_payload(
            name=name,
            status="failed" if failed else "completed" if event_type == "tool.complete" else "running",
            call_id=str(call_id) if call_id else f"{session_id}:{name}:unstable",
            has_stable_call_id=bool(call_id),
            input=args,
            preview=preview,
            result=public_result,
        )

    @staticmethod
    def _clarify_park_request_id(payload: dict[str, Any]) -> str | None:
        if str(payload.get("name") or "") != "clarify":
            return None
        candidates = (payload.get("result"), payload.get("error"))
        for result in candidates:
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except (TypeError, ValueError):
                    marker = result
                else:
                    marker = result.get("error") if isinstance(result, dict) else None
            elif isinstance(result, dict):
                marker = result.get("error")
            else:
                marker = None
            if not isinstance(marker, str):
                continue
            prefix = "__HERMES_CLARIFY_PARK__:"
            request_id = marker.removeprefix(prefix).strip()
            if request_id and request_id != marker and len(request_id) <= 128:
                return request_id
        return None

    @staticmethod
    def _usage_payload(usage: dict[str, Any]) -> dict[str, Any]:
        prompt = usage.get("input", usage.get("prompt", usage.get("prompt_tokens", 0)))
        completion = usage.get("output", usage.get("completion", usage.get("completion_tokens", 0)))
        total = usage.get("total", usage.get("total_tokens"))
        return {
            **usage,
            "prompt_tokens": int(prompt or 0),
            "completion_tokens": int(completion or 0),
            "total_tokens": int(total if total is not None else (prompt or 0) + (completion or 0)),
        }
