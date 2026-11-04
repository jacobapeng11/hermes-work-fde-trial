from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import mimetypes
import os
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlsplit

from fastapi import Cookie, FastAPI, Header, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from platform_supervisor.contracts import (
    ChannelFastLaneV2DeliveryAck,
    ChannelFastLaneV2DispositionAck,
    ChannelFastLaneV2DispositionFrame,
    ChannelFastLaneV2ResumeFrame,
    ChannelFastLaneV2SubmitFrame,
    RUNTIME_EDGE_WEBSOCKET_PROTOCOL,
    RuntimeCommandAck,
    RuntimeCommandEnvelope,
    RuntimeDashboardUpstreamErrorDetail,
    RuntimeEdgeError,
    RuntimeEdgeFollowupSubmitRequest,
    RuntimeEdgePrepareReady,
    RuntimeEdgePrepareRequest,
    RuntimeEdgeSessionHistory,
    RuntimeEdgeSessionHistoryRequest,
    RuntimeEdgeSessionQueue,
    RuntimeEdgeSessionQueueUpdated,
    RuntimeEdgeSessionQueueRequest,
    RuntimeEdgeRunAccepted,
    RuntimeEdgeRunQueued,
    RuntimeEdgeRunCancelAck,
    RuntimeEdgeRunCancelRequest,
    RuntimeEdgeDesktopCapabilitiesRequest,
    RuntimeEdgeLocalCapabilityRequest,
    RuntimeEdgeLocalCapabilityResult,
    RuntimeLocalCapabilityBridgeRequest,
    RuntimeEdgeRunResumeRequest,
    RuntimeEdgeRunSnapshot,
    RuntimeEdgeRunSubmitRequest,
    RuntimeEdgeRedirectDispositionBase,
    RuntimeEdgeFollowupApplied,
    RuntimeEdgeFollowupApplying,
    RuntimeEdgeFollowupContinued,
    RuntimeEdgeFollowupDispositionBase,
    RuntimeEdgeFollowupFailed,
    RuntimeEdgeFollowupReceived,
    SessionGrantClaims,
    canonical_payload_digest,
)
from platform_supervisor.admission_audit import AdmissionAuditReporter
from platform_supervisor.asset_contracts import (
    ProfileExecutionControlRequest,
    ProfileProjectionApplyRequest,
    RuntimeMcpRegistryApplyRequest,
)
from platform_supervisor.hermes.public_history_message import (
    normalize_internal_history_message,
)
from platform_supervisor.hermes.followup_history import project_followup_history
from platform_supervisor.hermes.dashboard_hot_channel import (
    DashboardChannelBusy,
)
from platform_supervisor.hermes.execution_adapter import HermesSessionPrepareError
from platform_supervisor.hermes.protocol import HermesProtocolError
from platform_supervisor.hermes.public_tool_payload import (
    sanitize_public_tool_history_message,
)
from platform_supervisor.configuration import (
    RuntimeConfigController,
    RuntimeConfigError,
    RuntimeConfigUnavailable,
)
from platform_supervisor.execution import (
    CapacityBusy,
    PrepareIdentity,
    RunManager,
    RunSubmission,
    SessionBusy,
    SessionCapacityBusy,
    SessionManager,
)
from platform_supervisor.execution.manager import RunNotFound
from platform_supervisor.health import RuntimeHealth
from platform_supervisor.security import RuntimeTokenError, RuntimeTokenVerifier
from platform_supervisor.storage import (
    IdempotencyConflict,
    RuntimeStorageLifecycle,
    SQLiteStore,
    StoredRun,
    StoredRedirect,
    StoredFollowupHistoryEntry,
)
from platform_supervisor.model_relay import (
    KanbanWorkerModelIdentity,
    KanbanWorkerModelRouter,
)
from platform_supervisor.profile_projection import ProfileProjectionError
from platform_supervisor.mcp_registry import McpRegistryError


logger = logging.getLogger(__name__)


# 给 RuntimeConfigError 配一条精准、可操作的兜底文案。
# 多数 RuntimeConfigError（CONFIG_REVISION_STALE 等）会被前端 runtime-edge-client
# 的 GRANT_REFRESH_REQUIRED_CODES 自动刷新 grant 并静默重试，用户不应看到；
# 真正露出给用户时，按错误码区分提示，避免一刀切地让用户"刷新授权"。
_RUNTIME_CONFIG_ERROR_MESSAGES: dict[str, str] = {
    "CONFIG_REVISION_STALE": "配置已更新，请重新发送消息",
    "CONFIG_BUNDLE_EXPIRED": "配置授权已过期，请刷新页面后重试",
    "CONFIG_BUNDLE_TARGET_MISMATCH": "配置与当前运行环境不匹配，请联系管理员",
    "CONFIG_BUNDLE_SIGNATURE_INVALID": "配置校验失败，请刷新页面后重试",
    "CONFIG_BUNDLE_DIGEST_MISMATCH": "配置完整性校验失败，请刷新页面后重试",
    "CONFIG_BUNDLE_FILE_COUNT_INVALID": "配置包含的文件数量不合法，请联系管理员",
    "CONFIG_BUNDLE_FILE_INVALID": "配置包含无效文件，请联系管理员",
    "CONFIG_BUNDLE_PATH_FORBIDDEN": "配置包含被禁止的路径，请联系管理员",
    "CONFIG_BUNDLE_PATH_DUPLICATE": "配置包含重复路径，请联系管理员",
    "CONFIG_BUNDLE_TOO_LARGE": "配置过大，请联系管理员",
    "CONFIG_BUNDLE_SYMLINK_FORBIDDEN": "配置不允许符号链接，请联系管理员",
    "CONFIG_BUNDLE_BASELINE_MISSING": "配置基线缺失，请联系管理员",
    "CURRENT_CONFIG_STATE_INVALID": "当前配置状态异常，请刷新页面后重试",
    "CURRENT_CONFIG_DIGEST_INVALID": "当前配置校验失败，请刷新页面后重试",
    "SUPERVISOR_PROTOCOL_TOO_OLD": "运行环境版本过低，请刷新页面后重试",
}


def _runtime_config_error_message(code: str) -> str:
    return _RUNTIME_CONFIG_ERROR_MESSAGES.get(
        code,
        "运行配置已变更，已自动重试；若仍失败请刷新页面",
    )


class ExternalConnectionBusy(RuntimeError):
    pass


class SessionControlAdapter(Protocol):
    async def capabilities(self) -> dict[str, Any]: ...

    async def dashboard_request(
        self,
        *,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def list_skills(self) -> list[dict[str, Any]]: ...

    async def reload_skills(self) -> dict[str, Any]: ...

    async def reload_skills_and_list(self) -> list[dict[str, Any]]: ...

    async def reload_mcp(self) -> None: ...

    async def reload_mcp_and_list(self) -> list[dict[str, Any]]: ...

    async def list_profile_mcp_servers(
        self,
        *,
        profile_name: str,
    ) -> list[dict[str, Any]]: ...

    async def apply_projected_config(
        self,
        *,
        generation: int,
        projection_hash: str,
        reload_skills: bool,
        reload_mcp: bool,
    ) -> dict[str, Any]: ...

    async def list_sessions(
        self,
        *,
        limit: int,
    ) -> dict[str, object]: ...

    async def get_session_messages(
        self,
        *,
        session_id: str,
        profile_name: str = "default",
    ) -> dict[str, object]: ...

    async def rename_session(
        self,
        *,
        session_id: str,
        title: str,
        profile_name: str = "default",
    ) -> dict[str, object]: ...

    async def delete_session(
        self,
        *,
        session_id: str,
        profile_name: str = "default",
    ) -> dict[str, object]: ...


class ProfileProjectionControl(Protocol):
    async def apply(self, request: ProfileProjectionApplyRequest): ...

    async def status(self, runtime_agent_profile_id: int): ...

    async def resolve_identity(self, identity, *, allow_pending: bool = False): ...


class McpRegistryControl(Protocol):
    async def apply(self, request): ...

    async def status(self): ...


@dataclass(frozen=True, slots=True)
class ChannelFastLaneV2ResumeBatch:
    disposition: ChannelFastLaneV2DispositionFrame
    events: tuple[dict[str, Any], ...] = ()


class ChannelFastLaneV2Router(Protocol):
    async def submit_turn(
        self,
        *,
        frame: ChannelFastLaneV2SubmitFrame,
        identity: PrepareIdentity,
        payload_digest: str,
    ) -> ChannelFastLaneV2DispositionFrame: ...

    async def resume_delivery(
        self,
        *,
        frame: ChannelFastLaneV2ResumeFrame,
    ) -> ChannelFastLaneV2ResumeBatch: ...

    async def acknowledge_disposition(
        self,
        *,
        frame: ChannelFastLaneV2DispositionAck,
    ) -> None: ...

    async def acknowledge_provider_delivery(
        self,
        *,
        frame: ChannelFastLaneV2DeliveryAck,
    ) -> int: ...


class ExternalConnectionGate:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._active = 0
        self._lock = asyncio.Lock()
        self._connections: set[WebSocket] = set()
        self._rejected_total = 0

    @property
    def active(self) -> int:
        return self._active

    @property
    def rejected_total(self) -> int:
        return self._rejected_total

    async def acquire(self, websocket: WebSocket) -> None:
        async with self._lock:
            if self._active >= self.limit:
                self._rejected_total += 1
                raise ExternalConnectionBusy
            self._active += 1
            self._connections.add(websocket)

    async def release(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._active = max(0, self._active - 1)
            self._connections.discard(websocket)

    async def close_all(
        self,
        *,
        code: int,
        reason: str,
    ) -> None:
        async with self._lock:
            connections = tuple(self._connections)
        await asyncio.gather(
            *(
                websocket.close(code=code, reason=reason)
                for websocket in connections
            ),
            return_exceptions=True,
        )


@dataclass(slots=True)
class RuntimeEdgeDependencies:
    runtime_id: str
    binding_id: str
    api_server_key: str
    token_verifier: RuntimeTokenVerifier
    store: SQLiteStore
    sessions: SessionManager
    runs: RunManager
    health: RuntimeHealth
    connections: ExternalConnectionGate
    config: RuntimeConfigController
    session_control: SessionControlAdapter | None = None
    workspace_dir: Path = Path("/workspace")
    storage_lifecycle: RuntimeStorageLifecycle | None = None
    admission_audit: AdmissionAuditReporter | None = None
    kanban_model_router: KanbanWorkerModelRouter | None = None
    channel_fast_lane_v2_router: ChannelFastLaneV2Router | None = None
    profile_projection: ProfileProjectionControl | None = None
    mcp_registry: McpRegistryControl | None = None


@dataclass(slots=True)
class _EdgeConnection:
    websocket: WebSocket
    user_id: int
    org_id: int
    admission_generation: int
    send_lock: asyncio.Lock
    selected_session_id: str | None = None
    local_capabilities: bool = False
    admission_audit: AdmissionAuditReporter | None = None

    def record_admission(
        self,
        *,
        operation: str,
        status: str,
        outcome: str,
        stage: str,
        **context: Any,
    ) -> str | None:
        if self.admission_audit is None:
            return None
        return self.admission_audit.record(
            operation=operation,
            status=status,
            outcome=outcome,
            stage=stage,
            **context,
        )

    async def send_model(self, model: Any) -> None:
        await self.send_json(
            model.model_dump(
                mode="json",
                exclude_none=not isinstance(
                    model,
                    (
                        RuntimeEdgeRedirectDispositionBase,
                        RuntimeEdgeFollowupDispositionBase,
                    ),
                ),
            )
        )

    async def send_json(self, payload: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.websocket.send_text(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            )

    async def send_bytes(self, payload: bytes) -> None:
        async with self.send_lock:
            await self.websocket.send_text(payload.decode("utf-8"))


@dataclass(slots=True)
class _PendingLocalCapability:
    connection: _EdgeConnection
    run_id: str
    capability: str
    future: asyncio.Future[RuntimeEdgeLocalCapabilityResult]


def attach_runtime_routes(
    app: FastAPI,
    dependencies: RuntimeEdgeDependencies,
) -> None:
    command_locks: dict[str, asyncio.Lock] = {}
    command_lock_users: dict[str, int] = {}
    command_locks_guard = asyncio.Lock()
    edge_connections: dict[int, _EdgeConnection] = {}
    run_connections: dict[str, _EdgeConnection] = {}
    session_connections: dict[str, _EdgeConnection] = {}
    session_active_runs: dict[str, str] = {}
    pending_local_capabilities: dict[str, _PendingLocalCapability] = {}

    async def publish_queue_update(
        session_id: str,
        summary: dict[str, int],
    ) -> None:
        update = RuntimeEdgeSessionQueueUpdated(
            session_id=session_id,
            **summary,
        )
        recipients = [
            connection
            for connection in tuple(edge_connections.values())
            if connection.selected_session_id == session_id
        ]
        await asyncio.gather(
            *(connection.send_model(update) for connection in recipients),
            return_exceptions=True,
        )

    dependencies.runs.set_queue_update_sink(publish_queue_update)
    control_cookie = hmac.new(
        dependencies.api_server_key.encode("utf-8"),
        b"runtime-control-cookie-v1",
        "sha256",
    ).hexdigest()

    @app.post("/auth/password-login")
    async def control_login(payload: dict[str, Any]) -> JSONResponse:
        valid = (
            payload.get("provider") == "basic"
            and payload.get("username") == "platform"
            and isinstance(payload.get("password"), str)
            and hmac.compare_digest(
                str(payload.get("password")),
                dependencies.api_server_key,
            )
        )
        if not valid:
            raise HTTPException(status_code=401, detail="invalid Runtime credential")
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "runtime_control",
            control_cookie,
            httponly=True,
            samesite="strict",
            secure=False,
            max_age=300,
        )
        return response

    @app.get("/api/status")
    async def control_status(
        runtime_control: str | None = Cookie(default=None),
    ) -> dict[str, Any]:
        _authorize_control_cookie(runtime_control, control_cookie)
        snapshot = dependencies.health.public_snapshot()
        return {
            "status": snapshot["state"],
            "version": "platform-supervisor-v1",
            "auth_required": True,
        }

    @app.get("/api/sessions")
    async def control_sessions(
        runtime_control: str | None = Cookie(default=None),
        limit: int = 1,
    ) -> dict[str, Any]:
        _authorize_control_cookie(runtime_control, control_cookie)
        if dependencies.session_control is None:
            raise HTTPException(status_code=503, detail="session control unavailable")
        return await dependencies.session_control.list_sessions(
            limit=max(1, min(limit, 500)),
        )

    @app.get("/api/sessions/{session_id}/messages")
    async def control_session_messages(
        session_id: str,
        profile: str = "default",
        runtime_control: str | None = Cookie(default=None),
    ) -> dict[str, object]:
        _authorize_control_cookie(runtime_control, control_cookie)
        if dependencies.session_control is None:
            raise HTTPException(status_code=503, detail="session control unavailable")
        return await dependencies.session_control.get_session_messages(
            session_id=session_id,
            profile_name=profile,
        )

    @app.patch("/api/sessions/{session_id}")
    async def control_rename_session(
        session_id: str,
        payload: dict[str, Any],
        runtime_control: str | None = Cookie(default=None),
    ) -> dict[str, object]:
        _authorize_control_cookie(runtime_control, control_cookie)
        if dependencies.session_control is None:
            raise HTTPException(status_code=503, detail="session control unavailable")
        title = str(payload.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=422, detail="session title is required")
        return await dependencies.session_control.rename_session(
            session_id=session_id,
            title=title,
            profile_name=str(payload.get("profile") or "default"),
        )

    @app.delete("/api/sessions/{session_id}")
    async def control_delete_session(
        session_id: str,
        profile: str = "default",
        runtime_control: str | None = Cookie(default=None),
    ) -> dict[str, object]:
        _authorize_control_cookie(runtime_control, control_cookie)
        if dependencies.session_control is None:
            raise HTTPException(status_code=503, detail="session control unavailable")
        return await dependencies.session_control.delete_session(
            session_id=session_id,
            profile_name=profile,
        )

    @app.put("/runtime-files/upload")
    async def upload_runtime_file(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        claims = _verify_file_transfer_ticket(
            dependencies,
            authorization,
            operation="upload",
        )
        if not await dependencies.store.consume_file_transfer_ticket(
            ticket_id=claims.ticket_id,
            operation=claims.operation,
            expires_at=claims.expires_at,
        ):
            raise HTTPException(
                status_code=401,
                detail="FileTransferTicket already consumed",
            )
        if (
            dependencies.storage_lifecycle is not None
            and not dependencies.storage_lifecycle.snapshot().uploads_allowed
        ):
            raise HTTPException(
                status_code=507,
                detail="PERSISTENT_STORAGE_UPLOAD_WATERMARK",
                headers={"Retry-After": "30"},
            )
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_bytes = int(content_length)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail="invalid content length",
                ) from exc
            if declared_bytes <= 0 or declared_bytes > claims.max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail="file is too large",
                )
        target = _ticket_file_target(
            dependencies.workspace_dir,
            claims.relative_path,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target = _ticket_file_target(
            dependencies.workspace_dir,
            claims.relative_path,
        )
        temporary = target.with_name(f".{target.name}.{claims.file_id}.part")
        digest = hashlib.sha256()
        size_bytes = 0
        file_handle = None
        try:
            temporary.unlink(missing_ok=True)
            file_handle = temporary.open("xb")
            async for chunk in request.stream():
                for offset in range(0, len(chunk), 1024 * 1024):
                    piece = chunk[offset : offset + 1024 * 1024]
                    size_bytes += len(piece)
                    if size_bytes > claims.max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail="file is too large",
                        )
                    digest.update(piece)
                    file_handle.write(piece)
            if size_bytes <= 0:
                raise HTTPException(status_code=400, detail="file is empty")
            if size_bytes != claims.max_bytes:
                raise HTTPException(
                    status_code=409,
                    detail="file size does not match ticket",
                )
            file_handle.flush()
            os.fsync(file_handle.fileno())
            file_handle.close()
            file_handle = None
            os.replace(temporary, target)
            _fsync_path_directory(target.parent)
            sha256_digest = digest.hexdigest()
            try:
                await dependencies.store.enqueue_projection(
                    projection_id=(
                        f"file:{claims.file_id}:ready:{sha256_digest[:32]}"
                    ),
                    projection_type="file.ready",
                    payload={
                        "file_id": claims.file_id,
                        "user_id": claims.user_id,
                        "org_id": claims.org_id,
                        "session_id": claims.session_id,
                        "relative_path": claims.relative_path,
                        "display_name": claims.display_name,
                        "mime_type": claims.mime_type,
                        "size_bytes": size_bytes,
                        "sha256": sha256_digest,
                    },
                    created_at=datetime.now(timezone.utc),
                )
            except Exception:
                target.unlink(missing_ok=True)
                raise
        except Exception:
            try:
                await dependencies.store.enqueue_projection(
                    projection_id=f"file:{claims.file_id}:failed",
                    projection_type="file.failed",
                    payload={
                        "file_id": claims.file_id,
                        "user_id": claims.user_id,
                        "org_id": claims.org_id,
                        "session_id": claims.session_id,
                        "relative_path": claims.relative_path,
                    },
                    created_at=datetime.now(timezone.utc),
                )
            except Exception:
                pass
            raise
        finally:
            if file_handle is not None:
                file_handle.close()
            temporary.unlink(missing_ok=True)
        return JSONResponse(
            {
                "file_id": claims.file_id,
                "status": "ready",
                "size_bytes": size_bytes,
                "sha256": digest.hexdigest(),
            }
        )

    @app.get("/runtime-files/download")
    async def download_runtime_file(
        authorization: str | None = Header(default=None),
    ) -> StreamingResponse:
        claims = _verify_file_transfer_ticket(
            dependencies,
            authorization,
            operation="download",
        )
        if not await dependencies.store.consume_file_transfer_ticket(
            ticket_id=claims.ticket_id,
            operation=claims.operation,
            expires_at=claims.expires_at,
        ):
            raise HTTPException(
                status_code=401,
                detail="FileTransferTicket already consumed",
            )
        target = _ticket_file_target(
            dependencies.workspace_dir,
            claims.relative_path,
        )
        try:
            size_bytes = target.stat().st_size
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="file not found") from exc
        if not target.is_file() or size_bytes > claims.max_bytes:
            raise HTTPException(status_code=409, detail="file changed")

        async def content():
            with target.open("rb") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk

        return StreamingResponse(
            content(),
            media_type=claims.mime_type or "application/octet-stream",
            headers={
                "Content-Length": str(size_bytes),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.websocket("/runtime-edge")
    async def runtime_edge(websocket: WebSocket) -> None:
        protocols = _websocket_protocols(websocket)
        token = _runtime_access_token(protocols)
        origin = str(websocket.headers.get("origin") or "")
        if (
            RUNTIME_EDGE_WEBSOCKET_PROTOCOL not in protocols
            or token is None
            or not origin
        ):
            await websocket.close(code=4401)
            return
        try:
            access = await dependencies.token_verifier.verify_runtime_access(
                token,
                origin=origin,
            )
        except RuntimeTokenError:
            await websocket.close(code=4401)
            return
        if not dependencies.health.ready:
            await websocket.close(code=1012, reason="runtime unavailable")
            return
        try:
            await dependencies.connections.acquire(websocket)
        except ExternalConnectionBusy:
            response = JSONResponse(
                {
                    "code": "RUNTIME_CONNECTION_CAPACITY_BUSY",
                    "message": "Runtime connection capacity is temporarily full",
                },
                status_code=503,
                headers={"Retry-After": "1"},
            )
            denial = getattr(websocket, "send_denial_response", None)
            if callable(denial):
                await denial(response)
            else:
                await websocket.close(code=4429)
            return

        await websocket.accept(subprotocol=RUNTIME_EDGE_WEBSOCKET_PROTOCOL)
        connection = _EdgeConnection(
            websocket=websocket,
            user_id=access.user_id,
            org_id=access.org_id,
            admission_generation=access.admission_generation,
            send_lock=asyncio.Lock(),
            admission_audit=dependencies.admission_audit,
        )
        edge_connections[id(connection)] = connection
        stream_tasks: set[asyncio.Task[None]] = set()
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    payload = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    await _send_error(connection, "INVALID_MESSAGE", "消息格式无效")
                    continue
                if not isinstance(payload, dict):
                    await _send_error(connection, "INVALID_MESSAGE", "消息格式无效")
                    continue
                message_type = payload.get("type")
                correlation = _edge_error_correlation(payload)
                audit_context: dict[str, Any] | None = None
                audit_stage: str | None = None
                try:
                    if message_type == "session.prepare":
                        audit_context = {
                            "operation": "session.prepare",
                            **correlation,
                        }
                        audit_stage = "edge.session_prepare.validate_request"
                        request = RuntimeEdgePrepareRequest.model_validate(payload)
                        audit_context["request_id"] = request.request_id
                        connection.record_admission(
                            status="received",
                            outcome="succeeded",
                            stage="edge.session_prepare.received",
                            **audit_context,
                        )
                        audit_stage = "edge.session_prepare.validate_grant"
                        grant = _verify_grant(dependencies, connection, request.session_grant)
                        audit_context.update(
                            _admission_grant_context(grant),
                        )
                        dependencies.config.require_revision(grant.config_revision)
                        connection.record_admission(
                            status="grant_validated",
                            outcome="succeeded",
                            stage="edge.session_prepare.validate_grant",
                            **audit_context,
                        )
                        try:
                            audit_stage = "edge.session_prepare.prepare_execution"
                            await _select_and_prepare(
                                dependencies,
                                connection,
                                grant,
                            )
                        except HermesSessionPrepareError as exc:
                            await _send_session_prepare_error(
                                connection,
                                request_id=request.request_id,
                                session_id=grant.session_id,
                                profile_id=grant.profile_id,
                                phase=exc.phase,
                                error=exc,
                                audit_context=audit_context,
                            )
                            continue
                        except HermesProtocolError as exc:
                            await _send_session_prepare_error(
                                connection,
                                request_id=request.request_id,
                                session_id=grant.session_id,
                                profile_id=grant.profile_id,
                                phase="unknown",
                                error=exc,
                                audit_context=audit_context,
                            )
                            continue
                        connection.record_admission(
                            status="execution_prepared",
                            outcome="succeeded",
                            stage="edge.session_prepare.prepare_execution",
                            **audit_context,
                        )
                        await connection.send_model(
                            RuntimeEdgePrepareReady(
                                request_id=request.request_id,
                                session_id=grant.session_id,
                                config_revision=grant.config_revision,
                            )
                        )
                        connection.local_capabilities = request.local_capabilities
                        session_connections[grant.session_id] = connection
                        connection.record_admission(
                            status="ready",
                            outcome="succeeded",
                            stage="edge.session_prepare.ready",
                            **audit_context,
                        )
                    elif message_type == "desktop.capabilities":
                        request = RuntimeEdgeDesktopCapabilitiesRequest.model_validate(
                            payload
                        )
                        connection.local_capabilities = request.local_capabilities
                    elif message_type == "session.history":
                        request = RuntimeEdgeSessionHistoryRequest.model_validate(
                            payload
                        )
                        grant = _verify_grant(
                            dependencies,
                            connection,
                            request.session_grant,
                        )
                        if dependencies.session_control is None:
                            raise RuntimeConfigUnavailable(
                                "SESSION_CONTROL_UNAVAILABLE"
                            )
                        history = (
                            await dependencies.session_control.get_session_messages(
                                session_id=grant.session_id,
                                profile_name=grant.profile_id,
                            )
                        )
                        history_sources = (
                            await dependencies.store.session_history_sources(
                                grant.session_id
                            )
                        )
                        followup_history = (
                            await dependencies.store.list_session_followup_history(
                                grant.session_id
                            )
                        )
                        await connection.send_model(
                            RuntimeEdgeSessionHistory(
                                request_id=request.request_id,
                                session_id=grant.session_id,
                                messages=_public_history_messages(
                                    history,
                                    sources_by_index=history_sources,
                                    followup_history=followup_history,
                                ),
                            )
                        )
                    elif message_type == "session.queue":
                        request = RuntimeEdgeSessionQueueRequest.model_validate(
                            payload
                        )
                        grant = _verify_grant(
                            dependencies,
                            connection,
                            request.session_grant,
                        )
                        summary = await dependencies.store.queue_summary(
                            grant.session_id
                        )
                        await connection.send_model(
                            RuntimeEdgeSessionQueue(
                                request_id=request.request_id,
                                session_id=grant.session_id,
                                **summary,
                            )
                        )
                    elif message_type in {"run.submit", "followup.submit"}:
                        is_followup = message_type == "followup.submit"
                        audit_context = {
                            "operation": message_type,
                            **correlation,
                        }
                        audit_stage = "edge.run_submit.validate_request"
                        request = (
                            RuntimeEdgeFollowupSubmitRequest.model_validate(payload)
                            if is_followup
                            else RuntimeEdgeRunSubmitRequest.model_validate(payload)
                        )
                        audit_context.update(
                            {
                                "client_message_id": request.client_message_id,
                                "attachment_count": len(request.attachment_file_ids),
                                "prompt_bytes": len(request.prompt.encode("utf-8")),
                            }
                        )
                        connection.record_admission(
                            status="received",
                            outcome="succeeded",
                            stage="edge.run_submit.received",
                            **audit_context,
                        )
                        audit_stage = "edge.run_submit.validate_grant"
                        grant = _verify_grant(dependencies, connection, request.session_grant)
                        audit_context.update(_admission_grant_context(grant))
                        dependencies.config.require_revision(grant.config_revision)
                        connection.record_admission(
                            status="grant_validated",
                            outcome="succeeded",
                            stage="edge.run_submit.validate_grant",
                            **audit_context,
                        )
                        audit_stage = "edge.run_submit.validate_request"
                        if len(request.prompt.encode("utf-8")) > grant.max_prompt_bytes:
                            await _send_error(
                                connection,
                                "PROMPT_TOO_LARGE",
                                "消息超过允许的大小",
                                **correlation,
                                audit_context=audit_context,
                                audit_stage=audit_stage,
                            )
                            continue
                        if await dependencies.store.is_revoked(
                            user_id=grant.user_id,
                            model_id=grant.model_id,
                            application_id=dependencies.runtime_id,
                        ):
                            await _send_error(
                                connection,
                                "RUNTIME_ACCESS_REVOKED",
                                "当前访问授权已撤销",
                                **correlation,
                                audit_context=audit_context,
                                audit_stage="edge.run_submit.check_revocation",
                            )
                            continue
                        connection.record_admission(
                            status="request_validated",
                            outcome="succeeded",
                            stage="edge.run_submit.validate_request",
                            **audit_context,
                        )
                        identity = _prepare_identity(grant)
                        runtime_prompt = _prompt_with_attachments(
                            grant,
                            request.attachment_file_ids,
                            request.prompt,
                        )
                        runtime_attachments = _runtime_attachments_for_request(
                            grant,
                            request.attachment_file_ids,
                        )
                        digest = canonical_payload_digest(
                            {
                                "attachment_file_ids": request.attachment_file_ids,
                                "config_revision": grant.config_revision,
                                "model_id": grant.model_id,
                                "profile_id": grant.profile_id,
                                "prompt": request.prompt,
                                "session_id": grant.session_id,
                                "user_id": grant.user_id,
                            }
                        )
                        audit_stage = "edge.run_submit.persist_dispatch_intent"
                        submission = await dependencies.runs.submit_interactive(
                            identity=identity,
                            client_message_id=request.client_message_id,
                            payload_digest=digest,
                            prompt=runtime_prompt,
                            audit_prompt=request.prompt,
                            attachment_file_ids=tuple(request.attachment_file_ids),
                            runtime_attachments=runtime_attachments,
                            redirect_eligible=is_followup,
                            expected_parent_run_id=(
                                request.parent_run_id if is_followup else None
                            ),
                            interrupt_parent=is_followup,
                            admission_generation=(
                                grant.admission_generation
                            ),
                            source="web",
                            source_detail=(
                                {
                                    "attachment_file_ids": list(
                                        request.attachment_file_ids
                                    )
                                }
                                if request.attachment_file_ids
                                else None
                            ),
                        )
                        disposition = _edge_interactive_disposition(submission)
                        if (
                            disposition.client_message_id
                            != request.client_message_id
                        ):
                            raise HermesProtocolError(
                                "interactive disposition identity mismatch"
                            )
                        if isinstance(
                            disposition,
                            (RuntimeEdgeRunQueued, RuntimeEdgeRunAccepted),
                        ):
                            audit_context["run_id"] = disposition.run_id
                            stream_run_id: str | None = disposition.run_id
                        else:
                            audit_context["run_id"] = (
                                disposition.parent_run_id
                            )
                            stream_run_id = (
                                disposition.continuation_run_id
                                if isinstance(
                                    disposition,
                                    RuntimeEdgeFollowupContinued,
                                )
                                else None
                            )
                        connection.record_admission(
                            status="accepted",
                            outcome="succeeded",
                            stage="edge.run_submit.persist_dispatch_intent",
                            **audit_context,
                        )
                        await connection.send_model(disposition)
                        if stream_run_id is not None:
                            run_connections[stream_run_id] = connection
                            session_active_runs[grant.session_id] = stream_run_id
                            connection.record_admission(
                                status="stream_started",
                                outcome="succeeded",
                                stage="edge.run_submit.stream_started",
                                **audit_context,
                            )
                            task = asyncio.create_task(
                                _stream_run(
                                    dependencies,
                                    connection,
                                    stream_run_id,
                                    after_seq=0,
                                    emit_accepted=not isinstance(
                                        disposition,
                                        RuntimeEdgeRunAccepted,
                                    ),
                                ),
                                name=f"edge-stream-{stream_run_id}",
                            )
                            stream_tasks.add(task)
                            task.add_done_callback(stream_tasks.discard)
                    elif message_type == "run.resume":
                        request = RuntimeEdgeRunResumeRequest.model_validate(payload)
                        stored = await _authorize_run(
                            dependencies,
                            connection,
                            request.run_id,
                        )
                        if stored.state in {"QUEUED", "ADMITTING"}:
                            await connection.send_model(
                                RuntimeEdgeRunQueued(
                                    run_id=stored.run_id,
                                    client_message_id=stored.client_message_id,
                                    enqueued_at=stored.enqueued_at,
                                    enqueue_seq=stored.enqueue_seq,
                                )
                            )
                        run_connections[request.run_id] = connection
                        session_active_runs[stored.session_id] = request.run_id
                        task = asyncio.create_task(
                            _stream_run(
                                dependencies,
                                connection,
                                request.run_id,
                                after_seq=request.after_seq,
                            ),
                            name=f"edge-resume-{request.run_id}",
                        )
                        stream_tasks.add(task)
                        task.add_done_callback(stream_tasks.discard)
                    elif message_type == "local.capability.result":
                        result = RuntimeEdgeLocalCapabilityResult.model_validate(payload)
                        pending = pending_local_capabilities.get(result.request_id)
                        if pending is None:
                            await _send_error(
                                connection,
                                "LOCAL_CAPABILITY_REQUEST_NOT_FOUND",
                                "本地能力请求已过期或不存在",
                                run_id=result.run_id,
                            )
                            continue
                        if pending.connection is not connection:
                            await _send_error(
                                connection,
                                "LOCAL_CAPABILITY_RESULT_FORBIDDEN",
                                "本地能力结果不属于当前连接",
                                run_id=result.run_id,
                            )
                            continue
                        if (
                            pending.run_id != result.run_id
                            or pending.capability != result.capability
                        ):
                            await _send_error(
                                connection,
                                "LOCAL_CAPABILITY_RESULT_INVALID",
                                "本地能力结果与请求不匹配",
                                run_id=result.run_id,
                            )
                            continue
                        pending_local_capabilities.pop(result.request_id, None)
                        if not pending.future.done():
                            pending.future.set_result(result)
                    elif message_type == "run.cancel":
                        request = RuntimeEdgeRunCancelRequest.model_validate(payload)
                        await _authorize_run(dependencies, connection, request.run_id)
                        cancel_status = await dependencies.runs.cancel(
                            request.run_id
                        )
                        await connection.send_model(
                            RuntimeEdgeRunCancelAck(
                                run_id=request.run_id,
                                status=cancel_status,
                            )
                        )
                    else:
                        await _send_error(
                            connection,
                            "UNSUPPORTED_MESSAGE",
                            "不支持的消息类型",
                            **correlation,
                        )
                except ValidationError:
                    await _send_error(
                        connection,
                        "INVALID_MESSAGE",
                        "消息字段无效",
                        **correlation,
                        audit_context=audit_context,
                        audit_stage=audit_stage,
                    )
                except RuntimeTokenError:
                    await _send_error(
                        connection,
                        "SESSION_GRANT_INVALID",
                        "会话授权已失效，请刷新后重试",
                        retryable=True,
                        **correlation,
                        audit_context=audit_context,
                        audit_stage=audit_stage,
                    )
                except CapacityBusy as exc:
                    error_code = str(exc)
                    await _send_error(
                        connection,
                        error_code,
                        (
                            "当前工作中的会话已达到 10 个，请等待其中一个完成或停止后再试"
                            if error_code == "PERSONAL_ACTIVE_SESSION_LIMIT"
                            else "当前请求较多，请稍后重试"
                        ),
                        **correlation,
                        audit_context=audit_context,
                        audit_stage=audit_stage,
                        exception_type=type(exc).__name__,
                    )
                except (SessionBusy, SessionCapacityBusy):
                    await _send_error(
                        connection,
                        "SESSION_BUSY",
                        "当前会话正在运行，请等待完成或手动结束后再试",
                        **correlation,
                        audit_context=audit_context,
                        audit_stage=audit_stage,
                    )
                except RuntimeConfigError as exc:
                    await _send_error(
                        connection,
                        str(exc),
                        _runtime_config_error_message(str(exc)),
                        retryable=True,
                        **correlation,
                        audit_context=audit_context,
                        audit_stage=audit_stage,
                        exception_type=type(exc).__name__,
                    )
                except IdempotencyConflict:
                    await _send_error(
                        connection,
                        "IDEMPOTENCY_CONFLICT",
                        "同一消息标识不能提交不同内容",
                        **correlation,
                        audit_context=audit_context,
                        audit_stage=audit_stage,
                    )
                except RunNotFound:
                    await _send_error(
                        connection,
                        "RUN_NOT_FOUND",
                        "未找到本次运行",
                        **correlation,
                        audit_context=audit_context,
                        audit_stage=audit_stage,
                    )
                except PermissionError:
                    await _send_error(
                        connection,
                        "RUN_FORBIDDEN",
                        "无权访问本次运行",
                        **correlation,
                        audit_context=audit_context,
                        audit_stage=audit_stage,
                    )
                except HermesProtocolError:
                    # A Dashboard session/materialization failure is scoped to
                    # the current request.  Keep the authenticated Runtime Edge
                    # connection alive so the client can retry instead of
                    # turning a recoverable Hermes error into a transport loss.
                    await _send_error(
                        connection,
                        "RUNTIME_SESSION_UNAVAILABLE",
                        "会话暂时不可用，请稍后重试",
                        retryable=True,
                        **correlation,
                        audit_context=audit_context,
                        audit_stage=audit_stage,
                    )
                except Exception as exc:
                    if audit_context is not None:
                        connection.record_admission(
                            status="unhandled_exception",
                            outcome="internal_error",
                            stage=audit_stage or "edge.websocket.unhandled_exception",
                            exception_type=type(exc).__name__,
                            **audit_context,
                        )
                    logger.error(
                        "runtime_edge_unhandled_command",
                        extra={
                            "event": "runtime_edge_unhandled_command",
                            "message_type": message_type,
                            "error_type": type(exc).__name__,
                            **correlation,
                        },
                    )
                    raise
        except WebSocketDisconnect:
            pass
        finally:
            edge_connections.pop(id(connection), None)
            for session_id, candidate in tuple(session_connections.items()):
                if candidate is connection:
                    session_connections.pop(session_id, None)
            for run_id, candidate in tuple(run_connections.items()):
                if candidate is connection:
                    run_connections.pop(run_id, None)
            for session_id, run_id in tuple(session_active_runs.items()):
                if run_connections.get(run_id) is not None:
                    continue
                # Backend-submitted runs (scheduled tasks) never appear in
                # run_connections. Keep their mapping while the run is
                # genuinely active so a desktop that reconnects mid-run can
                # still route local capabilities instead of falling to 409.
                orphaned = await dependencies.store.get_run(run_id)
                if (
                    orphaned is None
                    or orphaned.state in {"COMPLETED", "FAILED", "CANCELED"}
                ):
                    session_active_runs.pop(session_id, None)
            for request_id, pending in tuple(pending_local_capabilities.items()):
                if pending.connection is connection:
                    pending_local_capabilities.pop(request_id, None)
                    if not pending.future.done():
                        pending.future.cancel()
            for task in tuple(stream_tasks):
                task.cancel()
            await asyncio.gather(*stream_tasks, return_exceptions=True)
            if connection.selected_session_id is not None:
                await dependencies.sessions.deselect(connection.selected_session_id)
            await dependencies.connections.release(websocket)

    @app.post("/internal/v1/local-capabilities/request")
    async def request_local_capability(
        payload: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        """Bridge one in-process Hermes tool call to its owning desktop client.

        This endpoint is loopback-only in production: the child Hermes process
        receives the Supervisor API key, while the actual capability is still
        approved and executed by the authenticated desktop over Runtime Edge.
        """

        _authorize_internal(dependencies, authorization)
        try:
            request = RuntimeLocalCapabilityBridgeRequest.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="本地能力请求无效") from exc
        try:
            encoded_size = len(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="本地能力参数不可序列化") from exc
        if encoded_size > 256 * 1024:
            raise HTTPException(status_code=413, detail="本地能力参数过大")

        run_id = request.run_id or session_active_runs.get(request.session_id)
        connection = run_connections.get(run_id) if run_id else None
        if connection is None:
            candidate = session_connections.get(request.session_id)
            if candidate is not None and candidate.local_capabilities:
                connection = candidate
                run_id = session_active_runs.get(request.session_id)
        stored = await dependencies.store.get_run(run_id) if run_id else None
        if (
            stored is None
            or stored.session_id != request.session_id
            or stored.state in {"COMPLETED", "FAILED", "CANCELED"}
        ):
            raise HTTPException(status_code=409, detail="本地能力请求不属于活动运行")
        if (
            connection is None
            or not connection.local_capabilities
            or connection.selected_session_id != request.session_id
        ):
            # The desktop workspace folder is user-scoped, not session-scoped:
            # any online desktop of the same user can execute this capability.
            # That is what lets backend-submitted runs (scheduled tasks) land
            # locally whenever the desktop app is simply running — parked on
            # any conversation — with cloud-side generation only as the
            # fallback when no desktop of that user is connected at all.
            connection = next(
                (
                    candidate
                    for candidate in edge_connections.values()
                    if candidate.local_capabilities
                    and candidate.user_id == stored.user_id
                    and candidate.org_id == stored.org_id
                ),
                None,
            )
        if connection is None:
            raise HTTPException(status_code=409, detail="没有可用的桌面本地能力连接")
        if request.request_id in pending_local_capabilities:
            raise HTTPException(status_code=409, detail="本地能力请求 ID 已在使用")

        future: asyncio.Future[RuntimeEdgeLocalCapabilityResult] = (
            asyncio.get_running_loop().create_future()
        )
        pending_local_capabilities[request.request_id] = _PendingLocalCapability(
            connection=connection,
            run_id=run_id,
            capability=request.capability,
            future=future,
        )
        outbound = RuntimeEdgeLocalCapabilityRequest(
            request_id=request.request_id,
            run_id=run_id,
            session_id=request.session_id,
            capability=request.capability,
            args=request.args,
            reason=request.reason,
        )
        try:
            await connection.send_model(outbound)
            # The desktop may show a chat Clarify card before answering an
            # outside-workspace request. Keep this request alive long enough
            # for a normal user decision.
            result = await asyncio.wait_for(future, timeout=600)
        except asyncio.CancelledError as exc:
            raise HTTPException(status_code=503, detail="桌面本地能力连接已断开") from exc
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="桌面本地能力响应超时") from exc
        except (WebSocketDisconnect, RuntimeError, OSError) as exc:
            raise HTTPException(status_code=503, detail="桌面本地能力连接已断开") from exc
        finally:
            pending_local_capabilities.pop(request.request_id, None)
        return JSONResponse(result.model_dump(mode="json", exclude_none=True))

    @app.get("/internal/v1/desktop-status")
    async def desktop_status(
        session_id: str,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        """Report whether local-capability routing is available for a session.

        Loopback-only like the capability bridge: the Runtime desktop plugin
        polls this before cloud-side tool calls (server terminal / file
        writes / cloud browser) so its pre_tool_call gate can steer the agent
        toward the desktop_* equivalents. Online means any desktop client of
        this runtime currently holds a live connection with local
        capabilities — the capability bridge routes user-scoped, so a desktop
        parked on another conversation still serves the session. Only when no
        desktop is connected at all does this report offline, which keeps the
        gate disabled and cloud-side routing unchanged.
        """
        _authorize_internal(dependencies, authorization)
        connection = session_connections.get(session_id)
        online = bool(
            (connection is not None and connection.local_capabilities)
            or any(
                candidate.local_capabilities
                for candidate in edge_connections.values()
            )
        )
        return JSONResponse({"session_id": session_id, "online": online})

    @app.post("/internal/v1/control")
    async def internal_control(
        payload: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        """Execute bounded, low-frequency Runtime management operations."""

        _authorize_internal(dependencies, authorization)
        control = dependencies.session_control
        if control is None:
            raise HTTPException(status_code=503, detail="Runtime control unavailable")
        action = str(payload.get("action") or "").strip()
        params = payload.get("params")
        if not isinstance(params, dict):
            params = {}
        try:
            if action == "capabilities":
                result: Any = await control.capabilities()
            elif action == "dashboard_request":
                method = str(params.get("method") or "GET").upper()
                path = str(params.get("path") or "")
                _validate_dashboard_control_request(method, path)
                json_body = params.get("json_body")
                if json_body is not None and not isinstance(json_body, dict):
                    raise HTTPException(status_code=422, detail="json_body must be an object")
                result = await control.dashboard_request(
                    method=method,
                    path=path,
                    json_body=json_body,
                )
            elif action == "list_skills":
                result = await control.list_skills()
            elif action == "reload_skills":
                result = await control.reload_skills()
            elif action == "reload_skills_and_list":
                result = await control.reload_skills_and_list()
            elif action == "reload_mcp":
                await control.reload_mcp()
                result = {"status": "reloaded"}
            elif action == "reload_mcp_and_list":
                result = await control.reload_mcp_and_list()
            elif action == "list_profile_mcp_servers":
                profile_name = str(params.get("profile_name") or "").strip()
                _validate_control_profile_name(profile_name)
                result = await control.list_profile_mcp_servers(
                    profile_name=profile_name,
                )
            elif action == "apply_projected_config":
                result = await control.apply_projected_config(
                    generation=int(params.get("generation") or 0),
                    projection_hash=str(params.get("projection_hash") or ""),
                    reload_skills=bool(params.get("reload_skills", True)),
                    reload_mcp=bool(params.get("reload_mcp", True)),
                )
            elif action == "profile_projection.apply":
                if dependencies.profile_projection is None:
                    raise HTTPException(
                        status_code=503,
                        detail="Profile projection unavailable",
                    )
                request = ProfileProjectionApplyRequest.model_validate(params)
                result = (
                    await dependencies.profile_projection.apply(request)
                ).model_dump(mode="json")
            elif action == "profile_projection.status":
                if dependencies.profile_projection is None:
                    raise HTTPException(
                        status_code=503,
                        detail="Profile projection unavailable",
                    )
                result = (
                    await dependencies.profile_projection.status(
                        int(params.get("runtime_agent_profile_id") or 0)
                    )
                ).model_dump(mode="json")
            elif action == "profile_execution.control":
                if dependencies.profile_projection is None:
                    raise HTTPException(
                        status_code=503,
                        detail="Profile projection unavailable",
                    )
                request = ProfileExecutionControlRequest.model_validate(params)
                result = (
                    await dependencies.profile_projection.control(request)
                ).model_dump(mode="json")
            elif action == "runtime_mcp_registry.apply":
                if dependencies.mcp_registry is None:
                    raise HTTPException(
                        status_code=503,
                        detail="Runtime MCP Registry unavailable",
                    )
                request = RuntimeMcpRegistryApplyRequest.model_validate(params)
                result = (
                    await dependencies.mcp_registry.apply(request)
                ).model_dump(mode="json")
            elif action == "runtime_mcp_registry.status":
                if dependencies.mcp_registry is None:
                    raise HTTPException(
                        status_code=503,
                        detail="Runtime MCP Registry unavailable",
                    )
                result = (
                    await dependencies.mcp_registry.status()
                ).model_dump(mode="json")
            else:
                raise HTTPException(status_code=404, detail="Runtime control action not found")
        except ProfileProjectionError as exc:
            status_code = 404 if exc.code == "PROFILE_PROJECTION_NOT_FOUND" else 409
            raise HTTPException(status_code=status_code, detail=exc.code) from exc
        except McpRegistryError as exc:
            status_code = 404 if exc.code == "MCP_REGISTRY_NOT_FOUND" else 409
            raise HTTPException(status_code=status_code, detail=exc.code) from exc
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail="Profile projection request is invalid",
            ) from exc
        except DashboardChannelBusy as exc:
            raise HTTPException(status_code=409, detail="RUNTIME_CONTROL_BUSY") from exc
        except HermesProtocolError as exc:
            raise HTTPException(
                status_code=502,
                detail=_dashboard_upstream_error_detail(exc),
            ) from exc
        return JSONResponse({"result": result})

    @app.get("/internal/v1/files")
    async def list_internal_files(
        session_id: str,
        runtime_agent_profile_id: int | None = None,
        profile: str = "default",
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        _authorize_internal(dependencies, authorization)
        session_root = _internal_session_root(
            dependencies.workspace_dir,
            session_id=session_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
            profile=profile,
        )
        if not session_root.exists():
            return JSONResponse({"entries": []})
        entries: list[dict[str, Any]] = []
        for candidate in sorted(session_root.rglob("*")):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            relative = candidate.relative_to(dependencies.workspace_dir)
            if any(
                part in {".work", ".venv", "__pycache__"}
                or part.startswith((".", "~"))
                for part in relative.parts
            ):
                continue
            stat = candidate.stat()
            entries.append(
                {
                    "relative_path": PurePosixPath("workspace", *relative.parts).as_posix(),
                    "mime_type": mimetypes.guess_type(candidate.name)[0],
                    "size_bytes": stat.st_size,
                    "mtime": stat.st_mtime,
                }
            )
        return JSONResponse({"entries": entries})

    @app.get("/internal/v1/files/content")
    async def stream_internal_file(
        path: str,
        authorization: str | None = Header(default=None),
    ) -> StreamingResponse:
        _authorize_internal(dependencies, authorization)
        target = _ticket_file_target(dependencies.workspace_dir, path)
        if not target.is_file():
            raise HTTPException(status_code=404, detail="file not found")

        async def content():
            with target.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    yield chunk

        return StreamingResponse(
            content(),
            media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
            headers={
                "Content-Length": str(target.stat().st_size),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.put("/internal/v1/files/content")
    async def upload_internal_file(
        request: Request,
        path: str,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        _authorize_internal(dependencies, authorization)
        target = _ticket_file_target(dependencies.workspace_dir, path)
        if target.exists():
            raise HTTPException(status_code=409, detail="file already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        target = _ticket_file_target(dependencies.workspace_dir, path)
        temporary = target.with_name(f".{target.name}.internal.part")
        size_bytes = 0
        handle = None
        try:
            temporary.unlink(missing_ok=True)
            handle = temporary.open("xb")
            async for chunk in request.stream():
                size_bytes += len(chunk)
                if size_bytes > 100 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="file is too large")
                handle.write(chunk)
            if size_bytes <= 0:
                raise HTTPException(status_code=400, detail="file is empty")
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            os.replace(temporary, target)
            _fsync_path_directory(target.parent)
        finally:
            if handle is not None:
                handle.close()
            temporary.unlink(missing_ok=True)
        return JSONResponse(
            {
                "entry": {
                    "relative_path": path,
                    "mime_type": mimetypes.guess_type(target.name)[0],
                    "size_bytes": size_bytes,
                    "mtime": target.stat().st_mtime,
                }
            }
        )

    @app.post("/internal/v1/files/rename")
    async def rename_internal_file(
        payload: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        _authorize_internal(dependencies, authorization)
        source = _ticket_file_target(
            dependencies.workspace_dir,
            str(payload.get("source_relative_path") or ""),
        )
        target = _ticket_file_target(
            dependencies.workspace_dir,
            str(payload.get("target_relative_path") or ""),
        )
        if not source.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        if target.exists():
            raise HTTPException(status_code=409, detail="target already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        target = _ticket_file_target(
            dependencies.workspace_dir,
            str(payload.get("target_relative_path") or ""),
        )
        os.replace(source, target)
        _fsync_path_directory(target.parent)
        return JSONResponse({"renamed": True})

    @app.post("/internal/v1/files/delete")
    async def delete_internal_file(
        payload: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        _authorize_internal(dependencies, authorization)
        target = _ticket_file_target(
            dependencies.workspace_dir,
            str(payload.get("relative_path") or ""),
        )
        if target.exists() and not target.is_file():
            raise HTTPException(status_code=409, detail="file target is invalid")
        target.unlink(missing_ok=True)
        return JSONResponse({"deleted": True})

    @app.post("/internal/v1/commands")
    async def internal_command(
        envelope: RuntimeCommandEnvelope,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        _authorize_internal(dependencies, authorization)
        if envelope.runtime_id != dependencies.runtime_id:
            raise HTTPException(status_code=403, detail="runtime_id mismatch")
        if envelope.binding_id != dependencies.binding_id:
            raise HTTPException(status_code=409, detail="binding_id mismatch")
        if (
            envelope.deadline_at is not None
            and envelope.deadline_at <= datetime.now(timezone.utc)
        ):
            raise HTTPException(status_code=408, detail="command deadline exceeded")
        async with command_locks_guard:
            command_lock = command_locks.setdefault(
                envelope.command_id,
                asyncio.Lock(),
            )
            command_lock_users[envelope.command_id] = (
                command_lock_users.get(envelope.command_id, 0) + 1
            )
        try:
            async with command_lock:
                command_payload_digest = envelope.payload_digest
                if envelope.payload.type == "internal_turn":
                    command_payload_digest = canonical_payload_digest(
                        envelope.payload.model_dump(
                            mode="json",
                            exclude_none=True,
                            exclude={"channel_execution_lease"},
                        )
                    )
                command = await dependencies.store.persist_command(
                    command_id=envelope.command_id,
                    payload_digest=command_payload_digest,
                    command_type=envelope.payload.type,
                )
                if (
                    not command.created
                    and command.result is not None
                    and envelope.payload.type
                    not in {
                        "kanban_worker_route_activate",
                        "kanban_worker_route_revoke",
                    }
                ):
                    return JSONResponse(command.result)
                payload = envelope.payload
                status_code = 200
                headers: dict[str, str] | None = None
                retryable_before_admission = False
                try:
                    if payload.type == "prepare":
                        dependencies.config.require_revision(
                            payload.identity.config_revision
                        )
                        identity = _prepare_identity(
                            payload.identity,
                            runtime_id=dependencies.runtime_id,
                        )
                        if dependencies.profile_projection is not None:
                            identity = (
                                await dependencies.profile_projection.resolve_identity(
                                    identity,
                                    allow_pending=True,
                                )
                            )
                        if identity is not None:
                            await dependencies.sessions.prepare(identity)
                        ack = RuntimeCommandAck(
                            command_id=envelope.command_id,
                            state="completed",
                        )
                    elif payload.type == "internal_turn":
                        dependencies.config.require_revision(
                            payload.identity.config_revision
                        )
                        if (
                            len(payload.prompt.encode("utf-8"))
                            > payload.identity.max_prompt_bytes
                        ):
                            raise PermissionError("PROMPT_TOO_LARGE")
                        submission = await dependencies.runs.submit(
                            identity=_prepare_identity(
                                payload.identity,
                                runtime_id=dependencies.runtime_id,
                            ),
                            client_message_id=payload.client_message_id,
                            payload_digest=canonical_payload_digest(
                                payload.model_dump(
                                    mode="json",
                                    exclude_none=True,
                                    exclude={"channel_execution_lease"},
                                )
                            ),
                            prompt=payload.prompt,
                            source=payload.source,
                            runtime_attachments=[
                                item.model_dump(mode="json", exclude_none=True)
                                for item in payload.attachments
                            ],
                            run_id=payload.run_id,
                            source_detail=payload.source_detail,
                            scheduled_task_id=payload.scheduled_task_id,
                            scheduled_occurrence_key=(
                                payload.scheduled_occurrence_key
                            ),
                        )
                        # Backend-submitted turns (for example scheduled
                        # tasks) never pass through Edge run.submit. Record
                        # their active run so the local-capability bridge can
                        # route it to the desktop selected on that session.
                        session_active_runs[payload.identity.session_id] = (
                            submission.run.run_id
                        )
                        ack = RuntimeCommandAck(
                            command_id=envelope.command_id,
                            state="accepted",
                            run_id=submission.run.run_id,
                            result={
                                "admission_state": (
                                    "queued"
                                    if submission.run.state == "QUEUED"
                                    else "running"
                                ),
                                "coalesced": not submission.created,
                                "canonical_run_id": submission.run.run_id,
                            },
                        )
                    elif payload.type == "cancel":
                        await dependencies.runs.cancel(
                            payload.run_id,
                            clarify_request_id=payload.clarify_request_id,
                        )
                        ack = RuntimeCommandAck(
                            command_id=envelope.command_id,
                            state="completed",
                            run_id=payload.run_id,
                        )
                    elif payload.type == "cancel_active_runs":
                        canceled = await dependencies.runs.cancel_active_runs()
                        ack = RuntimeCommandAck(
                            command_id=envelope.command_id,
                            state="completed",
                            result={
                                "cancel_signals_sent": len(canceled),
                                "runs": canceled,
                            },
                        )
                    elif payload.type == "cancel_scheduled_candidate":
                        result = await dependencies.runs.cancel_scheduled_candidate(
                            session_id=payload.session_id,
                            scheduled_task_id=payload.scheduled_task_id,
                        )
                        ack = RuntimeCommandAck(
                            command_id=envelope.command_id,
                            state="completed",
                            result={"status": result},
                        )
                    elif payload.type == "drain":
                        await dependencies.runs.capacity.begin_drain("admin")
                        dependencies.health.mark_draining()
                        await dependencies.connections.close_all(
                            code=1012,
                            reason="runtime draining",
                        )
                        drained = await dependencies.runs.capacity.wait_until_empty(
                            timeout_seconds=payload.deadline_seconds,
                        )
                        if drained:
                            ack = RuntimeCommandAck(
                                command_id=envelope.command_id,
                                state="completed",
                                result={"state": "DRAINED"},
                            )
                        else:
                            await dependencies.runs.capacity.cancel_drain("admin")
                            dependencies.health.mark_hermes_ready()
                            ack = RuntimeCommandAck(
                                command_id=envelope.command_id,
                                state="rejected",
                                error_code="DRAIN_TIMEOUT",
                            )
                            status_code = 409
                    elif payload.type == "apply_config":
                        applied = await dependencies.config.apply(payload.bundle)
                        ack = RuntimeCommandAck(
                            command_id=envelope.command_id,
                            state="completed",
                            result={
                                "config_revision": applied.config_revision,
                                "config_digest": applied.config_digest,
                            },
                        )
                    elif payload.type == "revoke":
                        await dependencies.store.record_revocation(
                            command_id=envelope.command_id,
                            scope=payload.scope,
                            subject_id=payload.subject_id,
                            revoked_at=payload.revoked_at,
                            cancel_active_runs=payload.cancel_active_runs,
                        )
                        canceled_runs = (
                            await dependencies.runs.cancel_matching(
                                scope=payload.scope,
                                subject_id=payload.subject_id,
                            )
                            if payload.cancel_active_runs
                            else 0
                        )
                        ack = RuntimeCommandAck(
                            command_id=envelope.command_id,
                            state="completed",
                            result={"cancel_signals_sent": canceled_runs},
                        )
                    elif payload.type == "session_mutation":
                        if dependencies.session_control is None:
                            raise RuntimeConfigUnavailable(
                                "SESSION_CONTROL_UNAVAILABLE"
                            )
                        await dependencies.sessions.invalidate(
                            payload.session_id
                        )
                        if payload.operation == "rename":
                            result = (
                                await dependencies.session_control.rename_session(
                                    session_id=payload.session_id,
                                    title=str(payload.title),
                                    profile_name=payload.profile_id,
                                )
                            )
                        elif payload.operation == "delete":
                            result = (
                                await dependencies.session_control.delete_session(
                                    session_id=payload.session_id,
                                    profile_name=payload.profile_id,
                                )
                            )
                        else:
                            result = {"invalidated": True}
                        ack = RuntimeCommandAck(
                            command_id=envelope.command_id,
                            state="completed",
                            result=dict(result),
                        )
                    elif payload.type == "kanban_worker_route_activate":
                        if dependencies.kanban_model_router is None:
                            raise RuntimeConfigUnavailable(
                                "KANBAN_MODEL_ROUTER_UNAVAILABLE"
                            )
                        result = await dependencies.kanban_model_router.activate(
                            KanbanWorkerModelIdentity(
                                run_id=payload.run_id,
                                task_id=payload.task_id,
                                board=payload.board,
                                profile_id=payload.profile_id,
                                model_id=payload.model_id,
                                upstream_model_id=payload.upstream_model_id,
                                context_length=payload.context_length,
                                runtime_capabilities=(
                                    _runtime_capabilities_payload(payload)
                                    or {}
                                ),
                                max_output_tokens=payload.max_output_tokens,
                                org_id=payload.org_id,
                                provider_route=(
                                    payload.provider_route.model_dump(mode="json")
                                    if payload.provider_route is not None
                                    else None
                                ),
                                provider_route_digest=(
                                    payload.provider_route.digest
                                    if payload.provider_route is not None
                                    else None
                                ),
                            )
                        )
                        ack = RuntimeCommandAck(
                            command_id=envelope.command_id,
                            state="completed",
                            run_id=payload.run_id,
                            result=result,
                        )
                    elif payload.type == "kanban_worker_route_revoke":
                        if dependencies.kanban_model_router is None:
                            raise RuntimeConfigUnavailable(
                                "KANBAN_MODEL_ROUTER_UNAVAILABLE"
                            )
                        revoked = await dependencies.kanban_model_router.revoke(
                            KanbanWorkerModelIdentity(
                                run_id=payload.run_id,
                                task_id=payload.task_id,
                                board=payload.board,
                                profile_id=payload.profile_id,
                                model_id="revoke",
                                upstream_model_id="revoke",
                                runtime_capabilities={},
                                max_output_tokens=1,
                            )
                        )
                        ack = RuntimeCommandAck(
                            command_id=envelope.command_id,
                            state="completed",
                            run_id=payload.run_id,
                            result={"revoked": revoked},
                        )
                    else:
                        ack = RuntimeCommandAck(
                            command_id=envelope.command_id,
                            state="rejected",
                            error_code="COMMAND_NOT_IMPLEMENTED",
                        )
                except CapacityBusy as exc:
                    ack = RuntimeCommandAck(
                        command_id=envelope.command_id,
                        state="rejected",
                        error_code=str(exc),
                    )
                    status_code = 429
                    headers = {"Retry-After": "1"}
                    retryable_before_admission = True
                except (SessionBusy, SessionCapacityBusy):
                    ack = RuntimeCommandAck(
                        command_id=envelope.command_id,
                        state="rejected",
                        error_code="SESSION_BUSY",
                    )
                    status_code = 429
                    headers = {"Retry-After": "1"}
                    retryable_before_admission = True
                except RuntimeConfigUnavailable as exc:
                    ack = RuntimeCommandAck(
                        command_id=envelope.command_id,
                        state="rejected",
                        error_code=str(exc),
                    )
                    status_code = 503
                    headers = {"Retry-After": "1"}
                    retryable_before_admission = True
                except (PermissionError, RuntimeConfigError) as exc:
                    ack = RuntimeCommandAck(
                        command_id=envelope.command_id,
                        state="rejected",
                        error_code=str(exc),
                    )
                    status_code = 409
                result = ack.model_dump(mode="json", exclude_none=True)
                if retryable_before_admission:
                    await dependencies.store.abandon_command(
                        command_id=envelope.command_id,
                        payload_digest=command_payload_digest,
                    )
                else:
                    await dependencies.store.complete_command(
                        command_id=envelope.command_id,
                        state=ack.state,
                        result=result,
                    )
                return JSONResponse(
                    result,
                    status_code=status_code,
                    headers=headers,
                )
        finally:
            async with command_locks_guard:
                remaining = command_lock_users[envelope.command_id] - 1
                if remaining == 0:
                    command_lock_users.pop(envelope.command_id, None)
                    command_locks.pop(envelope.command_id, None)
                else:
                    command_lock_users[envelope.command_id] = remaining

    @app.get("/internal/v1/runs/{run_id}/events")
    async def stream_internal_run_events(
        run_id: str,
        after_seq: int = Query(default=0, ge=0),
        authorization: str | None = Header(default=None),
    ) -> StreamingResponse:
        _authorize_internal(dependencies, authorization)
        if await dependencies.store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found")

        async def content():
            try:
                async for chunk in dependencies.runs.subscribe(
                    run_id,
                    after_seq=after_seq,
                ):
                    yield chunk + b"\n"
                return
            except RunNotFound:
                snapshot = await dependencies.store.load_run_snapshot(
                    run_id,
                    after_seq=after_seq,
                )
                if snapshot is None:
                    return
                for item in snapshot.events:
                    envelope = {
                        "schema_version": 1,
                        "type": "run.event",
                        "run_id": item.run_id,
                        "seq": item.seq,
                        "event_type": item.event_type,
                        "emitted_at": item.occurred_at.isoformat(),
                        "data": item.payload,
                    }
                    yield json.dumps(
                        envelope,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ).encode("utf-8") + b"\n"

        return StreamingResponse(
            content(),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "private, no-store",
                "X-Accel-Buffering": "no",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/internal/v1/runs/{run_id}")
    async def internal_run(
        run_id: str,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        _authorize_internal(dependencies, authorization)
        snapshot = await dependencies.store.load_run_snapshot(run_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return JSONResponse(_snapshot_payload(snapshot))

    @app.get("/internal/v1/runs/{run_id}/metadata")
    async def internal_run_metadata(
        run_id: str,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        """Return restart verification metadata without loading event bodies."""
        _authorize_internal(dependencies, authorization)
        run = await dependencies.store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        metadata = await dependencies.store.load_run_metadata(run_id)
        assert metadata is not None
        return JSONResponse(metadata)


def _websocket_protocols(websocket: WebSocket) -> tuple[str, ...]:
    raw = str(websocket.headers.get("sec-websocket-protocol") or "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _public_history_messages(
    payload: dict[str, object],
    *,
    sources_by_index: dict[int, dict[str, Any]] | None = None,
    followup_history: list[StoredFollowupHistoryEntry] | None = None,
) -> list[dict[str, Any]]:
    raw_messages = payload.get("messages", payload.get("data", []))
    if not isinstance(raw_messages, list):
        return []
    sourced_messages: list[dict[str, Any]] = []
    compacted_handoff = False
    for index, raw in enumerate(raw_messages):
        if not isinstance(raw, dict):
            continue
        sourced = dict(raw)
        if index == 0:
            content = sourced.get("content", sourced.get("text"))
            compacted_handoff = (
                isinstance(content, str)
                and content.lstrip().startswith("[CONTEXT COMPACTION")
            )
        if (
            str(sourced.get("role") or "") == "user"
            and sources_by_index is not None
            and index in sources_by_index
        ):
            sourced["source"] = _public_history_source(
                sources_by_index[index]
            )
        sourced_messages.append(sourced)
    # Hermes keeps the current user row after the compacted execution tail.
    # Once the handoff row is hidden, that tail would otherwise render before
    # the user's prompt. Move only the first user row across that known
    # compaction boundary; ordinary histories retain their original order.
    if compacted_handoff:
        first_user = next(
            (index for index, item in enumerate(sourced_messages)
             if index > 0
             if str(item.get("role") or "") == "user"),
            None,
        )
        if first_user is not None and first_user > 0:
            user_message = sourced_messages.pop(first_user)
            sourced_messages.insert(0, user_message)
    projected_messages = project_followup_history(
        sourced_messages,
        followup_history or [],
    )
    messages: list[dict[str, Any]] = []
    for index, raw in enumerate(projected_messages):
        normalized = normalize_internal_history_message(dict(raw), index)
        if normalized is None:
            continue
        if str(normalized.get("role") or "") == "tool":
            normalized = sanitize_public_tool_history_message(
                normalized,
                index,
            )
        messages.append(normalized)
    return messages


def _public_history_source(source: dict[str, Any]) -> dict[str, Any]:
    public = dict(source)
    runtime_attachments = public.pop("runtime_attachments", None)
    if isinstance(runtime_attachments, list):
        public.setdefault(
            "attachments",
            [
                {
                    "attachmentId": int(item["file_id"]),
                    "displayName": str(item["display_name"]),
                    **(
                        {"mimeType": str(item["mime_type"])}
                        if item.get("mime_type") is not None
                        else {}
                    ),
                }
                for item in runtime_attachments
                if isinstance(item, dict)
                and str(item.get("file_id") or "").isdigit()
            ],
        )
    return public


def _runtime_access_token(protocols: tuple[str, ...]) -> str | None:
    values = [
        item.removeprefix("runtime-access.")
        for item in protocols
        if item.startswith("runtime-access.")
    ]
    return values[0] if len(values) == 1 and values[0] else None


def _bearer_ticket(authorization: str | None) -> str:
    scheme, _, token = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="FileTransferTicket required")
    return token


def _verify_file_transfer_ticket(
    dependencies: RuntimeEdgeDependencies,
    authorization: str | None,
    *,
    operation: str,
):
    try:
        return dependencies.token_verifier.verify_file_transfer(
            _bearer_ticket(authorization),
            operation=operation,
        )
    except RuntimeTokenError as exc:
        raise HTTPException(
            status_code=401,
            detail="FileTransferTicket invalid",
        ) from exc


_DASHBOARD_CONTROL_PREFIXES = (
    "/api/config",
    "/api/files",
    "/api/mcp/servers",
    "/api/model/info",
    "/api/plugins/kanban",
    "/api/profiles",
)
_DASHBOARD_CONTROL_EXACT_POST_PATHS = frozenset({"/api/sessions/import"})
_DASHBOARD_CONTROL_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_SESSION_PREPARE_ERROR_PHASES = frozenset(
    {
        "live_execution_release",
        "model_route",
        "session_import",
        "dashboard_prewarm",
        "profile_prompt_refresh",
        "unknown",
    }
)


def _validate_dashboard_control_request(method: str, path: str) -> None:
    parsed = urlsplit(path)
    if (
        method not in _DASHBOARD_CONTROL_METHODS
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or ".." in PurePosixPath(parsed.path).parts
        or not any(
            parsed.path == prefix or parsed.path.startswith(f"{prefix}/")
            for prefix in _DASHBOARD_CONTROL_PREFIXES
        )
        and not (
            method == "POST"
            and parsed.path in _DASHBOARD_CONTROL_EXACT_POST_PATHS
        )
    ):
        raise HTTPException(status_code=403, detail="Dashboard control path is forbidden")


def _dashboard_upstream_error_detail(
    error: HermesProtocolError,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    status_code = error.status_code
    path = error.path
    if (
        isinstance(status_code, int)
        and not isinstance(status_code, bool)
        and 400 <= status_code <= 599
        and isinstance(path, str)
        and len(path) <= 2048
    ):
        try:
            _validate_dashboard_control_request("GET", path)
        except HTTPException:
            pass
        else:
            fields = {
                "upstream_status_code": status_code,
                "upstream_path": path,
            }
    return RuntimeDashboardUpstreamErrorDetail(**fields).model_dump(
        mode="json",
        exclude_none=True,
    )


def _validate_control_profile_name(profile_name: str) -> None:
    if profile_name == "default":
        return
    prefix = "expert-"
    suffix = profile_name.removeprefix(prefix)
    if (
        not profile_name.startswith(prefix)
        or not suffix.isdigit()
        or int(suffix) <= 0
    ):
        raise HTTPException(status_code=422, detail="invalid Runtime profile name")


def _internal_session_root(
    workspace_dir: Path,
    *,
    session_id: str,
    runtime_agent_profile_id: int | None,
    profile: str,
) -> Path:
    safe_session_id = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in session_id
    )
    if not safe_session_id:
        raise HTTPException(status_code=400, detail="invalid session id")
    root = workspace_dir
    if (
        profile != "default"
        and runtime_agent_profile_id is not None
        and runtime_agent_profile_id > 0
    ):
        root = root / "experts" / str(runtime_agent_profile_id)
    target = root / "sessions" / safe_session_id
    try:
        target.resolve().relative_to(workspace_dir.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid session id") from exc
    return target


def _ticket_file_target(workspace_dir: Path, relative_path: str) -> Path:
    path = PurePosixPath(relative_path)
    lower_parts = tuple(part.casefold() for part in path.parts)
    if (
        path.is_absolute()
        or len(path.parts) < 2
        or path.parts[0] != "workspace"
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(
            part in {"home", "log", "logs", ".work", ".venv", "__pycache__"}
            for part in lower_parts
        )
        or any(part in {".env", "config.yaml"} for part in lower_parts)
        or path.suffix.casefold() in {".db", ".sqlite", ".sqlite3"}
    ):
        raise HTTPException(status_code=400, detail="invalid file path")
    root = workspace_dir.resolve()
    target = root.joinpath(*path.parts[1:])
    resolved_parent = target.parent.resolve()
    try:
        resolved_parent.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid file path") from exc
    if target.exists() and target.is_symlink():
        raise HTTPException(status_code=409, detail="invalid file target")
    return target


def _fsync_path_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _edge_interactive_disposition(
    submission: RunSubmission | StoredRedirect,
) -> (
    RuntimeEdgeRunQueued
    | RuntimeEdgeRunAccepted
    | RuntimeEdgeFollowupDispositionBase
):
    if isinstance(submission, RunSubmission):
        if submission.run.accepted_at is not None:
            return RuntimeEdgeRunAccepted(
                run_id=submission.run.run_id,
                client_message_id=submission.run.client_message_id,
                accepted_at=submission.run.accepted_at,
            )
        return RuntimeEdgeRunQueued(
            run_id=submission.run.run_id,
            client_message_id=submission.run.client_message_id,
            enqueued_at=submission.run.enqueued_at,
            enqueue_seq=submission.run.enqueue_seq,
        )
    if not isinstance(submission, StoredRedirect):
        raise HermesProtocolError(
            "interactive submission returned a non-durable disposition"
        )
    redirect = submission
    common: dict[str, Any] = {
        "followup_id": redirect.redirect_id,
        "client_message_id": redirect.client_message_id,
        "parent_run_id": redirect.parent_run_id,
        "input_admission_seq": redirect.input_admission_seq,
    }
    if redirect.state == "WAITING_AGENT_READY":
        return RuntimeEdgeFollowupReceived(
            **common,
            dispatch_group_id=None,
            dispatch_group_position=None,
            event_seq=redirect.pending_event_seq,
        )
    if redirect.state in {
        "WAITING_REDIRECT_BOUNDARY",
        "DISPATCH_INTENT",
        "RPC_ACKNOWLEDGED",
        "ISOLATING_UNCERTAIN",
    }:
        return RuntimeEdgeFollowupApplying(
            **common,
            dispatch_group_id=redirect.dispatch_group_id,
            dispatch_group_position=redirect.dispatch_group_position,
            event_seq=redirect.accepted_event_seq or redirect.pending_event_seq,
        )
    if redirect.state == "CANONICAL_CONSUMED":
        if (
            redirect.consumed_event_seq is None
            or redirect.dispatch_group_id is None
            or redirect.dispatch_group_position is None
            or redirect.start_after_seq is None
        ):
            raise HermesProtocolError("redirect consumed disposition is incomplete")
        return RuntimeEdgeFollowupApplied(
            **common,
            dispatch_group_id=redirect.dispatch_group_id,
            dispatch_group_position=redirect.dispatch_group_position,
            event_seq=redirect.consumed_event_seq,
            proof_kind="canonical_history",
        )
    terminal_event_seq = redirect.terminal_disposition_event_seq
    if terminal_event_seq is None:
        raise HermesProtocolError("redirect terminal disposition is incomplete")
    terminal_common = {
        **common,
        "dispatch_group_id": redirect.dispatch_group_id,
        "dispatch_group_position": redirect.dispatch_group_position,
        "event_seq": terminal_event_seq,
    }
    if redirect.state == "PROMOTED_TO_RUN":
        if redirect.promoted_run_id is None:
            raise HermesProtocolError("redirect promotion disposition is incomplete")
        return RuntimeEdgeFollowupContinued(
            **terminal_common,
            continuation_run_id=redirect.promoted_run_id,
            member_client_message_ids=list(
                redirect.continuation_client_message_ids
                or (redirect.client_message_id,)
            ),
        )
    if redirect.state == "CANCELED_WITH_PARENT":
        return RuntimeEdgeFollowupFailed(
            **terminal_common,
            error_code="FOLLOWUP_PARENT_CANCELED",
        )
    if redirect.state == "SUBMISSION_UNCERTAIN":
        if (
            redirect.dispatch_group_id is None
            or redirect.dispatch_group_position is None
        ):
            raise HermesProtocolError("redirect uncertain disposition is incomplete")
        return RuntimeEdgeFollowupFailed(
            **terminal_common,
            error_code="FOLLOWUP_DELIVERY_UNRESOLVED",
        )
    raise HermesProtocolError("redirect state has no public disposition")


def _verify_grant(
    dependencies: RuntimeEdgeDependencies,
    connection: _EdgeConnection,
    token: str,
) -> SessionGrantClaims:
    grant = dependencies.token_verifier.verify_session_grant(token)
    if (
        grant.user_id != connection.user_id
        or grant.org_id != connection.org_id
        or grant.admission_generation != connection.admission_generation
    ):
        raise RuntimeTokenError("SessionGrant identity does not match connection")
    return grant


async def _select_and_prepare(
    dependencies: RuntimeEdgeDependencies,
    connection: _EdgeConnection,
    grant: SessionGrantClaims,
) -> None:
    if (
        connection.selected_session_id is not None
        and connection.selected_session_id != grant.session_id
    ):
        await dependencies.sessions.deselect(connection.selected_session_id)
        connection.selected_session_id = None
    identity = _prepare_identity(grant)
    if dependencies.profile_projection is not None:
        identity = await dependencies.profile_projection.resolve_identity(
            identity,
            allow_pending=True,
        )
    if identity is not None:
        await dependencies.sessions.prepare(identity)
    if connection.selected_session_id is None:
        await dependencies.sessions.select(grant.session_id)
        connection.selected_session_id = grant.session_id


def _prepare_identity(
    identity: Any,
    *,
    runtime_id: str | None = None,
) -> PrepareIdentity:
    return PrepareIdentity(
        runtime_id=runtime_id or identity.runtime_id,
        session_id=identity.session_id,
        profile_id=identity.profile_id,
        model_id=identity.model_id,
        upstream_model_id=getattr(identity, "upstream_model_id", None),
        context_length=getattr(identity, "context_length", 128_000),
        runtime_capabilities=_runtime_capabilities_payload(identity),
        config_revision=identity.config_revision,
        admission_generation=identity.admission_generation,
        max_output_tokens=identity.max_output_tokens,
        user_id=identity.user_id,
        org_id=identity.org_id,
        runtime_agent_profile_id=identity.runtime_agent_profile_id,
        application_agent_id=getattr(identity, "application_agent_id", None),
        profile_desired_revision=identity.profile_desired_revision,
        profile_applied_revision=identity.profile_applied_revision,
        profile_digest=identity.profile_digest,
        skill_digest=identity.skill_digest,
        mcp_digest=identity.mcp_digest,
        knowledge_digest=identity.knowledge_digest,
        provider_route=(
            identity.provider_route.model_dump(mode="json")
            if getattr(identity, "provider_route", None) is not None
            else None
        ),
        provider_route_digest=(
            identity.provider_route.digest
            if getattr(identity, "provider_route", None) is not None
            else None
        ),
    )


def _runtime_capabilities_payload(identity: Any) -> dict[str, str] | None:
    value = getattr(identity, "runtime_capabilities", None)
    if value is None:
        return None
    dump = getattr(value, "model_dump", None)
    return (
        dump(exclude_none=True)
        if callable(dump)
        else {
            key: item
            for key, item in value.items()
            if item is not None
        }
    )


def _prompt_with_attachments(
    grant: SessionGrantClaims,
    requested_file_ids: list[str],
    prompt: str,
) -> str:
    if len(set(requested_file_ids)) != len(requested_file_ids):
        raise PermissionError("ATTACHMENT_GRANT_MISMATCH")
    granted = {item.file_id: item for item in grant.attachments}
    if any(file_id not in granted for file_id in requested_file_ids):
        raise PermissionError("ATTACHMENT_GRANT_MISMATCH")
    if not requested_file_ids:
        return prompt
    image_ids = [
        file_id for file_id in requested_file_ids
        if str(granted[file_id].mime_type or "").startswith("image/")
    ]
    other_ids = [file_id for file_id in requested_file_ids if file_id not in set(image_ids)]
    image_lines = "\n".join(f"- {granted[file_id].display_name}" for file_id in image_ids)
    other_lines = "\n".join(
        (
            f"- {granted[file_id].display_name}: "
            f"{granted[file_id].relative_path}"
        )
        for file_id in other_ids
    )
    sections = []
    if image_ids:
        sections.append(
            "Image attachments are already delivered to your vision channel "
            "by image.attach. Answer visual questions directly from them. "
            "Do not call vision_analyze, terminal, file/workspace tools, or "
            "session_search for these images unless the user explicitly "
            "requests file-level inspection or cross-session comparison:\n"
            + image_lines
        )
    if other_ids:
        sections.append(
            "Non-image files (read from the current workspace when needed):\n"
            + other_lines
        )
    return (
        f"{prompt}\n\n"
        "<platform_attachments>\n"
        "The user attached these files. Read them from the current workspace "
        "when needed.\n"
        "Attachment-specific delivery instructions below override the "
        "default workspace instruction.\n"
        + "\n".join(sections) + "\n"
        "</platform_attachments>"
    )


def _runtime_attachments_for_request(
    grant: SessionGrantClaims,
    requested_file_ids: list[str],
) -> list[dict[str, object]]:
    granted = {item.file_id: item for item in grant.attachments}
    return [
        granted[file_id].model_dump(mode="json", exclude_none=True)
        for file_id in requested_file_ids
    ]


async def _authorize_run(
    dependencies: RuntimeEdgeDependencies,
    connection: _EdgeConnection,
    run_id: str,
) -> StoredRun:
    run = await dependencies.store.get_run(run_id)
    if run is None:
        raise RunNotFound(run_id)
    if run.user_id != connection.user_id or run.org_id != connection.org_id:
        raise PermissionError
    return run


async def _stream_run(
    dependencies: RuntimeEdgeDependencies,
    connection: _EdgeConnection,
    run_id: str,
    *,
    after_seq: int,
    emit_accepted: bool = True,
) -> None:
    try:
        admitted = await dependencies.runs.wait_until_admitted(run_id)
        if emit_accepted and admitted.accepted_at is not None:
            await connection.send_model(
                RuntimeEdgeRunAccepted(
                    run_id=admitted.run_id,
                    client_message_id=admitted.client_message_id,
                    accepted_at=admitted.accepted_at,
                )
            )
        async for chunk in dependencies.runs.subscribe(run_id, after_seq=after_seq):
            await connection.send_bytes(chunk)
        return
    except RunNotFound:
        pass
    snapshot = await dependencies.store.load_run_snapshot(
        run_id,
        after_seq=after_seq,
    )
    if snapshot is not None:
        await connection.send_json(_snapshot_payload(snapshot))


def _snapshot_payload(snapshot: Any) -> dict[str, Any]:
    return RuntimeEdgeRunSnapshot(
        run_id=snapshot.run.run_id,
        through_seq=max(
            snapshot.answer_through_seq,
            max((item.seq for item in snapshot.events), default=0),
        ),
        status=snapshot.run.state,
        answer=snapshot.answer,
        events=[
            {
                "run_id": item.run_id,
                "seq": item.seq,
                "event_type": item.event_type,
                "emitted_at": item.occurred_at,
                "data": item.payload,
            }
            for item in snapshot.events
        ],
    ).model_dump(mode="json")


async def _send_error(
    connection: _EdgeConnection,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    request_id: str | None = None,
    client_message_id: str | None = None,
    run_id: str | None = None,
    phase: str | None = None,
    audit_context: dict[str, Any] | None = None,
    audit_stage: str | None = None,
    exception_type: str | None = None,
    upstream_status: int | None = None,
    upstream_path: str | None = None,
) -> None:
    audit_event_id: str | None = None
    if audit_context is not None:
        audit_event_id = connection.record_admission(
            status="rejected",
            outcome="rejected",
            stage=audit_stage or f"edge.{audit_context['operation']}.rejected",
            error_code=code,
            exception_type=exception_type,
            upstream_status=upstream_status,
            upstream_path=upstream_path,
            **audit_context,
        )
    await connection.send_model(
        RuntimeEdgeError(
            code=code,
            message=message,
            retryable=retryable,
            request_id=request_id,
            client_message_id=client_message_id,
            run_id=run_id,
            phase=phase,
            audit_event_id=audit_event_id,
        )
    )


async def _send_session_prepare_error(
    connection: _EdgeConnection,
    *,
    request_id: str,
    session_id: str,
    profile_id: str,
    phase: str,
    error: HermesProtocolError,
    audit_context: dict[str, Any],
) -> None:
    safe_phase = phase if phase in _SESSION_PREPARE_ERROR_PHASES else "unknown"
    logger.warning(
        "runtime_session_prepare_failed",
        extra={
            "event": "runtime_session_prepare_failed",
            "phase": safe_phase,
            "request_id": request_id,
            "session_id": session_id,
            "profile_id": profile_id,
            "exception_type": type(error.__cause__ or error).__name__,
            "upstream_status_code": error.status_code,
            "upstream_path": _safe_upstream_path(error.path),
        },
    )
    await _send_error(
        connection,
        "RUNTIME_SESSION_PREPARE_FAILED",
        "会话暂时不可用，请稍后重试",
        retryable=True,
        request_id=request_id,
        phase=safe_phase,
        audit_context=audit_context,
        audit_stage=f"edge.session_prepare.{safe_phase}",
        exception_type=type(error.__cause__ or error).__name__,
        upstream_status=error.status_code,
        upstream_path=_safe_upstream_path(error.path),
    )


def _safe_upstream_path(path: str | None) -> str | None:
    if not isinstance(path, str) or len(path) > 512:
        return None
    parsed_path = urlsplit(path).path
    if not parsed_path.startswith("/"):
        return None
    return parsed_path


def _edge_error_correlation(payload: dict[str, Any]) -> dict[str, str]:
    correlation: dict[str, str] = {}
    for field in ("request_id", "client_message_id", "run_id"):
        value = payload.get(field)
        if isinstance(value, str) and 8 <= len(value) <= 128:
            correlation[field] = value
    return correlation


def _admission_grant_context(grant: SessionGrantClaims) -> dict[str, Any]:
    return {
        "session_id": grant.session_id,
        "profile_id": grant.profile_id,
        "application_agent_id": getattr(grant, "application_agent_id", None),
        "model_id": grant.model_id,
        "config_revision": grant.config_revision,
        "admission_generation": grant.admission_generation,
    }


def _authorize_internal(
    dependencies: RuntimeEdgeDependencies,
    authorization: str | None,
) -> None:
    expected = f"Bearer {dependencies.api_server_key}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid Runtime credential")


def _authorize_control_cookie(
    value: str | None,
    expected: str,
) -> None:
    if value is None or not hmac.compare_digest(value, expected):
        raise HTTPException(status_code=401, detail="Runtime control login required")
