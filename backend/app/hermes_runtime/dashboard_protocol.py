from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, BinaryIO

import httpx

from app.hermes_runtime.protocol import (
    HermesProtocolError,
    RuntimeDashboardUpstreamError,
    shared_runtime_http_client,
)
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.schemas.asset_projection import (
    ProfileExecutionControlRequest,
    ProfileExecutionControlResponse,
    ProfileProjectionApplyRequest,
    ProfileProjectionApplyResponse,
    RuntimeMcpRegistryApplyRequest,
    RuntimeMcpRegistryApplyResponse,
)


class RuntimeControlBusy(HermesProtocolError):
    """Supervisor rejected a management mutation while its Runtime is busy."""


@dataclass(frozen=True)
class ManagedRuntimeArtifact:
    relative_path: str
    mime_type: str | None
    size_bytes: int
    mtime: float | None = None


class HermesDashboardProtocolAdapter:
    """Thin Backend client for Supervisor-owned Runtime control operations.

    Hermes Dashboard transport, WebSockets, HotChannel state, prompt execution,
    and artifact filesystem access all live inside the Runtime Pod.  This class
    only sends bounded control requests to Supervisor port 9120.
    """

    def __init__(
        self,
        *,
        session_workspace_root: str | None = None,
    ) -> None:
        self.session_workspace_root = (
            session_workspace_root.rstrip("/")
            if session_workspace_root
            else None
        )

    async def health(self, endpoint: RuntimeEndpoint) -> dict[str, Any]:
        try:
            response = await (await shared_runtime_http_client()).get(
                self._url(endpoint, "/readyz"),
                headers=self._headers(endpoint),
                timeout=endpoint.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise HermesProtocolError(
                "Runtime Supervisor is unavailable",
                status_code=503,
                path="/readyz",
            ) from exc
        payload = self._response_json(response, path="/readyz")
        state = str(payload.get("state") or "")
        if state != "HERMES_READY":
            raise HermesProtocolError(
                "Runtime Supervisor is not ready",
                status_code=response.status_code,
                path="/readyz",
            )
        return {
            "status": "ok",
            "version": "platform-supervisor-v1",
            "platform": "hermes-agent",
            "transport": "runtime-supervisor",
            "http": "ready",
            "auth": "ready",
        }

    async def capabilities(self, endpoint: RuntimeEndpoint) -> dict[str, Any]:
        result = await self._action(endpoint, "capabilities")
        if not isinstance(result, dict):
            raise HermesProtocolError("Runtime capabilities result is invalid")
        return result

    async def dashboard_json_request(
        self,
        endpoint: RuntimeEndpoint,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = await self._action(
            endpoint,
            "dashboard_request",
            {
                "method": method,
                "path": path,
                "json_body": json_body,
            },
        )
        if not isinstance(result, dict):
            raise HermesProtocolError(
                "Runtime Dashboard control result is invalid",
                path=path,
            )
        return result

    async def list_skills(
        self,
        endpoint: RuntimeEndpoint,
    ) -> list[dict[str, Any]]:
        return self._object_list(await self._action(endpoint, "list_skills"))

    async def reload_skills(
        self,
        endpoint: RuntimeEndpoint,
    ) -> dict[str, Any]:
        result = await self._action(endpoint, "reload_skills")
        if not isinstance(result, dict):
            raise HermesProtocolError("Runtime Skill reload result is invalid")
        return result

    async def reload_skills_and_list(
        self,
        endpoint: RuntimeEndpoint,
    ) -> list[dict[str, Any]]:
        return self._object_list(
            await self._action(endpoint, "reload_skills_and_list")
        )

    async def reload_mcp(self, endpoint: RuntimeEndpoint) -> None:
        await self._action(endpoint, "reload_mcp")

    async def reload_mcp_and_list(
        self,
        endpoint: RuntimeEndpoint,
    ) -> list[dict[str, Any]]:
        return self._object_list(
            await self._action(endpoint, "reload_mcp_and_list")
        )

    async def apply_profile_projection(
        self,
        endpoint: RuntimeEndpoint,
        request: ProfileProjectionApplyRequest,
    ) -> ProfileProjectionApplyResponse:
        result = await self._action(
            endpoint,
            "profile_projection.apply",
            request.model_dump(mode="json"),
        )
        try:
            return ProfileProjectionApplyResponse.model_validate(result)
        except ValueError as exc:
            raise HermesProtocolError(
                "Runtime Profile projection result is invalid"
            ) from exc

    async def get_profile_projection_status(
        self,
        endpoint: RuntimeEndpoint,
        *,
        runtime_agent_profile_id: int,
    ) -> ProfileProjectionApplyResponse:
        result = await self._action(
            endpoint,
            "profile_projection.status",
            {"runtime_agent_profile_id": runtime_agent_profile_id},
        )
        try:
            return ProfileProjectionApplyResponse.model_validate(result)
        except ValueError as exc:
            raise HermesProtocolError(
                "Runtime Profile projection status is invalid"
            ) from exc

    async def control_profile_execution(
        self,
        endpoint: RuntimeEndpoint,
        request: ProfileExecutionControlRequest,
    ) -> ProfileExecutionControlResponse:
        result = await self._action(
            endpoint,
            "profile_execution.control",
            request.model_dump(mode="json"),
        )
        try:
            return ProfileExecutionControlResponse.model_validate(result)
        except ValueError as exc:
            raise HermesProtocolError(
                "Runtime Profile execution control result is invalid"
            ) from exc

    async def apply_runtime_mcp_registry(
        self,
        endpoint: RuntimeEndpoint,
        request: RuntimeMcpRegistryApplyRequest,
    ) -> RuntimeMcpRegistryApplyResponse:
        result = await self._action(
            endpoint,
            "runtime_mcp_registry.apply",
            request.model_dump(mode="json"),
        )
        try:
            return RuntimeMcpRegistryApplyResponse.model_validate(result)
        except ValueError as exc:
            raise HermesProtocolError(
                "Runtime MCP Registry result is invalid"
            ) from exc

    async def get_runtime_mcp_registry_status(
        self,
        endpoint: RuntimeEndpoint,
    ) -> RuntimeMcpRegistryApplyResponse:
        result = await self._action(endpoint, "runtime_mcp_registry.status")
        try:
            return RuntimeMcpRegistryApplyResponse.model_validate(result)
        except ValueError as exc:
            raise HermesProtocolError(
                "Runtime MCP Registry status is invalid"
            ) from exc

    async def list_profile_mcp_servers(
        self,
        endpoint: RuntimeEndpoint,
        *,
        profile_name: str,
    ) -> list[dict[str, Any]]:
        return self._object_list(
            await self._action(
                endpoint,
                "list_profile_mcp_servers",
                {"profile_name": profile_name},
            )
        )

    async def apply_config(
        self,
        endpoint: RuntimeEndpoint,
        **kwargs: Any,
    ) -> dict[str, Any]:
        result = await self._action(
            endpoint,
            "apply_projected_config",
            {
                "generation": int(kwargs.get("generation") or 0),
                "projection_hash": str(
                    kwargs.get("expected_projection_hash") or ""
                ),
                "reload_skills": bool(kwargs.get("reload_skills", True)),
                "reload_mcp": bool(kwargs.get("reload_mcp", True)),
            },
        )
        if not isinstance(result, dict):
            raise HermesProtocolError("Runtime config apply result is invalid")
        return result

    async def list_session_artifacts(
        self,
        endpoint: RuntimeEndpoint,
        *,
        runtime_session_id: str,
        runtime_agent_profile_id: int | None = None,
        hermes_profile_name: str = "default",
    ) -> list[ManagedRuntimeArtifact]:
        response = await (await shared_runtime_http_client()).get(
            self._url(endpoint, "/internal/v1/files"),
            headers=self._headers(endpoint),
            params={
                "session_id": runtime_session_id,
                "runtime_agent_profile_id": runtime_agent_profile_id,
                "profile": hermes_profile_name,
            },
            timeout=endpoint.timeout_seconds,
        )
        payload = self._response_json(response, path="/internal/v1/files")
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise HermesProtocolError("Runtime artifact listing is invalid")
        artifacts: list[ManagedRuntimeArtifact] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            relative_path = str(entry.get("relative_path") or "")
            parts = PurePosixPath(relative_path).parts
            if not relative_path or any(
                part in {".work", ".venv", "__pycache__"}
                or part.startswith((".", "~"))
                for part in parts
            ):
                continue
            artifacts.append(
                ManagedRuntimeArtifact(
                    relative_path=relative_path,
                    mime_type=(
                        str(entry["mime_type"])
                        if entry.get("mime_type")
                        else None
                    ),
                    size_bytes=max(0, int(entry.get("size_bytes") or 0)),
                    mtime=(
                        float(entry["mtime"])
                        if entry.get("mtime") is not None
                        else None
                    ),
                )
            )
        return artifacts

    async def stream_managed_artifact(
        self,
        endpoint: RuntimeEndpoint,
        *,
        relative_path: str,
    ) -> AsyncIterator[bytes]:
        client = await shared_runtime_http_client()
        async with client.stream(
            "GET",
            self._url(endpoint, "/internal/v1/files/content"),
            headers=self._headers(endpoint),
            params={"path": self._validated_relative_path(relative_path)},
            timeout=endpoint.timeout_seconds,
        ) as response:
            self._raise_for_status(response, path="/internal/v1/files/content")
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
        del filename

        async def body() -> AsyncIterator[bytes]:
            while True:
                chunk = await asyncio.to_thread(content.read, 1024 * 1024)
                if not chunk:
                    return
                yield chunk

        response = await (await shared_runtime_http_client()).put(
            self._url(endpoint, "/internal/v1/files/content"),
            headers={
                **self._headers(endpoint),
                "Content-Type": mime_type or "application/octet-stream",
            },
            params={"path": self._validated_relative_path(relative_path)},
            content=body(),
            timeout=endpoint.timeout_seconds,
        )
        payload = self._response_json(
            response,
            path="/internal/v1/files/content",
        )
        entry = payload.get("entry")
        if not isinstance(entry, dict):
            raise HermesProtocolError("Runtime artifact upload is invalid")
        return ManagedRuntimeArtifact(
            relative_path=str(entry.get("relative_path") or relative_path),
            mime_type=(
                str(entry["mime_type"])
                if entry.get("mime_type")
                else mime_type
            ),
            size_bytes=max(0, int(entry.get("size_bytes") or 0)),
            mtime=(
                float(entry["mtime"])
                if entry.get("mtime") is not None
                else None
            ),
        )

    async def rename_managed_artifact(
        self,
        endpoint: RuntimeEndpoint,
        *,
        source_relative_path: str,
        target_relative_path: str,
    ) -> None:
        await self._post_json(
            endpoint,
            "/internal/v1/files/rename",
            {
                "source_relative_path": self._validated_relative_path(
                    source_relative_path
                ),
                "target_relative_path": self._validated_relative_path(
                    target_relative_path
                ),
            },
        )

    async def delete_managed_artifact(
        self,
        endpoint: RuntimeEndpoint,
        *,
        relative_path: str,
    ) -> None:
        await self._post_json(
            endpoint,
            "/internal/v1/files/delete",
            {"relative_path": self._validated_relative_path(relative_path)},
        )

    async def _action(
        self,
        endpoint: RuntimeEndpoint,
        action: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        payload = await self._post_json(
            endpoint,
            "/internal/v1/control",
            {"action": action, "params": params or {}},
        )
        if "result" not in payload:
            raise HermesProtocolError("Runtime control result is missing")
        return payload["result"]

    async def _post_json(
        self,
        endpoint: RuntimeEndpoint,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await (await shared_runtime_http_client()).post(
            self._url(endpoint, path),
            headers=self._headers(endpoint),
            json=payload,
            timeout=endpoint.timeout_seconds,
        )
        return self._response_json(response, path=path)

    @classmethod
    def _response_json(
        cls,
        response: httpx.Response,
        *,
        path: str,
    ) -> dict[str, Any]:
        cls._raise_for_status(response, path=path)
        try:
            payload = response.json()
        except ValueError as exc:
            raise HermesProtocolError(
                "Runtime control returned invalid JSON",
                status_code=response.status_code,
                path=path,
            ) from exc
        if not isinstance(payload, dict):
            raise HermesProtocolError(
                "Runtime control returned a non-object payload",
                status_code=response.status_code,
                path=path,
            )
        return payload

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, path: str) -> None:
        if 200 <= response.status_code < 300:
            return
        detail = ""
        upstream_error = None
        try:
            payload = response.json()
            if isinstance(payload, dict):
                raw_detail = payload.get("detail")
                if isinstance(raw_detail, str):
                    detail = raw_detail
                elif response.status_code == 502:
                    upstream_error = RuntimeDashboardUpstreamError.parse(
                        raw_detail
                    )
        except ValueError:
            pass
        if upstream_error is not None:
            raise HermesProtocolError(
                upstream_error.message,
                status_code=upstream_error.status_code,
                path=upstream_error.path,
            )
        error_type = (
            RuntimeControlBusy
            if response.status_code == 409 and detail == "RUNTIME_CONTROL_BUSY"
            else HermesProtocolError
        )
        raise error_type(
            detail or f"Runtime control failed with status {response.status_code}",
            status_code=response.status_code,
            path=path,
        )

    @staticmethod
    def _headers(endpoint: RuntimeEndpoint) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {endpoint.api_key}"}
        if endpoint.endpoint_host_header:
            headers["Host"] = endpoint.endpoint_host_header
        return headers

    @staticmethod
    def _url(endpoint: RuntimeEndpoint, path: str) -> str:
        return f"{endpoint.endpoint_base_url.rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def _object_list(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not all(
            isinstance(item, dict) for item in value
        ):
            raise HermesProtocolError("Runtime control list result is invalid")
        return value

    @staticmethod
    def _validated_relative_path(value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or len(path.parts) < 2
            or path.parts[0] != "workspace"
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise HermesProtocolError("Runtime artifact path is invalid")
        return path.as_posix()
