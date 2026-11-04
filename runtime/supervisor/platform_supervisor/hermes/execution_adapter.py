from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import hashlib
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from platform_supervisor.execution.sessions import PrepareIdentity
from platform_supervisor.hermes.dashboard_hot_channel import (
    ActiveRunControlPeerRegistry,
    DashboardHotChannelIdentity,
    DashboardHotChannelManager,
    DashboardInterruptSettleResult,
)
from platform_supervisor.hermes.dashboard_protocol import (
    HermesDashboardProtocolAdapter,
)
from platform_supervisor.hermes.schemas import RuntimeEndpoint
from platform_supervisor.hermes.protocol import HermesProtocolError
from platform_supervisor.hermes.events import PlatformStreamEvent
from platform_supervisor.model_relay.routing import SessionModelRouter
from platform_supervisor.settings import SupervisorSettings


_SESSION_PREPARE_PHASES = frozenset(
    {
        "live_execution_release",
        "model_route",
        "session_import",
        "dashboard_prewarm",
        "profile_prompt_refresh",
    }
)

logger = logging.getLogger(__name__)
_ACTIVE_MCP_RELOAD_TEST_MARKER = Path(
    "/opt/hermes-platform/allow-active-mcp-reload-test"
)


class HermesSessionPrepareError(HermesProtocolError):
    """A safe, phase-labelled Dashboard failure while preparing a session."""

    def __init__(self, phase: str, cause: HermesProtocolError) -> None:
        super().__init__(
            "Hermes session preparation failed",
            rpc_code=cause.rpc_code,
            rpc_method=cause.rpc_method,
            rpc_data=cause.rpc_data,
            status_code=cause.status_code,
            path=cause.path,
        )
        self.phase = phase if phase in _SESSION_PREPARE_PHASES else "unknown"


class HermesExecutionAdapter:
    """Supervisor-owned facade over the unmodified Dashboard protocol."""

    def __init__(
        self,
        settings: SupervisorSettings,
        *,
        dashboard_session_token: str,
        model_router: SessionModelRouter,
        control_peers: ActiveRunControlPeerRegistry | None = None,
        protocol: HermesDashboardProtocolAdapter | None = None,
    ) -> None:
        self.settings = settings
        self.model_router = model_router
        self.endpoint = RuntimeEndpoint(
            runtime_workspace_id=self._stable_positive_int(settings.RUNTIME_ID),
            runtime_id=settings.RUNTIME_ID,
            endpoint_base_url=(
                f"http://{settings.HERMES_DASHBOARD_HOST}:"
                f"{settings.HERMES_DASHBOARD_PORT}"
            ),
            endpoint_host_header=(
                f"{settings.HERMES_DASHBOARD_HOST}:"
                f"{settings.HERMES_DASHBOARD_PORT}"
            ),
            api_key=dashboard_session_token,
            timeout_seconds=120,
            coordination_scope_key=f"runtime:{settings.RUNTIME_ID}",
        )
        self.hot_channels = DashboardHotChannelManager(
            idle_grace_seconds=settings.APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS,
            max_independent_slots=settings.max_resident_sessions,
        )
        self.protocol = protocol or HermesDashboardProtocolAdapter(
            session_workspace_root=str(settings.WORKSPACE_DIR),
            hot_channels=self.hot_channels,
            control_peers=control_peers,
            allow_active_mcp_reload=_ACTIVE_MCP_RELOAD_TEST_MARKER.is_file(),
        )
        self.control_peers = control_peers or getattr(
            self.protocol,
            "control_peers",
            None,
        )
        self._route_prepare_proofs: dict[str, tuple[str, object]] = {}
        self._agent_generation_advancer: (
            Callable[[str], Awaitable[int]] | None
        ) = None

    def bind_agent_generation_advancer(
        self,
        advancer: Callable[[str], Awaitable[int]],
    ) -> None:
        self._agent_generation_advancer = advancer

    async def _advance_agent_generation_after_mcp_reload(self) -> None:
        if self._agent_generation_advancer is None:
            logger.error("runtime_mcp_reload_agent_generation_advancer_unbound")
            return
        count = await self._agent_generation_advancer("mcp-reload")
        logger.warning(
            "runtime_mcp_reload_agent_generation_advanced count=%s",
            count,
        )

    async def _run_mcp_reload_operation(
        self,
        operation: Awaitable[Any],
    ) -> Any:
        try:
            result = await operation
        except BaseException:
            try:
                await self._advance_agent_generation_after_mcp_reload()
            except Exception:
                logger.exception(
                    "runtime_mcp_reload_uncertain_agent_generation_failed"
                )
            raise
        await self._advance_agent_generation_after_mcp_reload()
        return result

    async def health(self) -> None:
        await self.protocol.initialize_default_session_store(self.endpoint)
        await asyncio.gather(
            self.protocol.health(self.endpoint),
            self.protocol.prewarm(self.endpoint),
        )

    async def capabilities(self) -> dict[str, Any]:
        return await self.protocol.capabilities(self.endpoint)

    async def dashboard_request(
        self,
        *,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.protocol.dashboard_json_request(
            self.endpoint,
            method=method,
            path=path,
            json_body=json_body,
        )

    async def read_profile_config(self, profile_name: str) -> dict[str, Any]:
        normalized = self._hermes_profile_name(profile_name)
        encoded = quote(normalized, safe="")
        path = f"/api/config/raw?profile={encoded}"
        payload = await self.protocol.dashboard_json_request(
            self.endpoint,
            method="GET",
            path=path,
        )
        raw = payload.get("yaml")
        if not isinstance(raw, str):
            raise HermesProtocolError(
                "Hermes raw config response is invalid",
                path="/api/config/raw",
            )
        try:
            document = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise HermesProtocolError(
                "Hermes raw config YAML is invalid",
                path="/api/config/raw",
            ) from exc
        if document is None and normalized != "default":
            # A valid named Profile can predate its first config.yaml.  The
            # raw Dashboard endpoint represents that state as an empty YAML
            # document; projection owns creating the first complete config.
            return {}
        if not isinstance(document, dict):
            raise HermesProtocolError(
                "Hermes raw config root is invalid",
                path="/api/config/raw",
            )
        return document

    async def write_profile_config(
        self,
        profile_name: str,
        document: dict[str, Any],
    ) -> None:
        normalized = self._hermes_profile_name(profile_name)
        encoded = quote(normalized, safe="")
        await self.protocol.dashboard_json_request(
            self.endpoint,
            method="PUT",
            path=f"/api/config/raw?profile={encoded}",
            json_body={
                "yaml_text": yaml.safe_dump(
                    document,
                    sort_keys=False,
                    allow_unicode=True,
                ),
                "profile": normalized,
            },
        )
        observed = await self.read_profile_config(normalized)
        if observed != document:
            raise HermesProtocolError(
                "Hermes raw config write verification failed",
                path="/api/config/raw",
            )

    async def ensure_profile(
        self,
        *,
        profile_name: str,
        description: str,
        soul_content: str,
    ) -> None:
        normalized = self._hermes_profile_name(profile_name)
        if normalized != "default":
            payload = await self.protocol.dashboard_json_request(
                self.endpoint,
                method="GET",
                path="/api/profiles",
            )
            profiles = payload.get("profiles")
            if not isinstance(profiles, list) or not all(
                isinstance(item, dict) for item in profiles
            ):
                raise HermesProtocolError(
                    "Hermes Profile list response is invalid",
                    path="/api/profiles",
                )
            if normalized not in {
                str(item.get("name") or "").strip() for item in profiles
            }:
                await self.protocol.dashboard_json_request(
                    self.endpoint,
                    method="POST",
                    path="/api/profiles",
                    json_body={
                        "name": normalized,
                        "description": description,
                        "clone_all": False,
                        "no_skills": True,
                    },
                )
            encoded = quote(normalized, safe="")
            await self.protocol.dashboard_json_request(
                self.endpoint,
                method="PUT",
                path=f"/api/profiles/{encoded}/description",
                json_body={"description": description},
            )
            await self.protocol.dashboard_json_request(
                self.endpoint,
                method="PUT",
                path=f"/api/profiles/{encoded}/soul",
                json_body={"content": soul_content},
            )

    async def list_skills(self) -> list[dict[str, Any]]:
        return await self.protocol.list_skills(self.endpoint)

    async def reload_skills(self) -> dict[str, Any]:
        return await self.protocol.reload_skills(self.endpoint)

    async def reload_skills_and_list(self) -> list[dict[str, Any]]:
        return await self.protocol.reload_skills_and_list(self.endpoint)

    async def reload_mcp(self) -> None:
        await self._run_mcp_reload_operation(
            self.protocol.reload_mcp(self.endpoint)
        )

    async def reload_mcp_and_list(self) -> list[dict[str, Any]]:
        return await self._run_mcp_reload_operation(
            self.protocol.reload_mcp_and_list(self.endpoint)
        )

    async def reload_mcp_registry_and_list(self) -> list[dict[str, Any]]:
        return await self._run_mcp_reload_operation(
            self.protocol.reload_mcp_unconditional_and_list(
                self.endpoint
            )
        )

    async def list_mcp_registry_servers(self) -> list[dict[str, Any]]:
        return await self.protocol.list_mcp_registry_servers(self.endpoint)

    async def list_profile_mcp_servers(
        self,
        *,
        profile_name: str,
    ) -> list[dict[str, Any]]:
        return await self.protocol.list_profile_mcp_servers(
            self.endpoint,
            profile_name=profile_name,
        )

    async def apply_projected_config(
        self,
        *,
        generation: int,
        projection_hash: str,
        reload_skills: bool,
        reload_mcp: bool,
    ) -> dict[str, Any]:
        operation = self.protocol.apply_config(
            self.endpoint,
            generation=generation,
            expected_projection_hash=projection_hash,
            reload_skills=reload_skills,
            reload_mcp=reload_mcp,
            invalidate_agents=True,
        )
        if reload_mcp:
            return await self._run_mcp_reload_operation(operation)
        return await operation

    async def prepare(self, identity: PrepareIdentity) -> None:
        self._route_prepare_proofs.pop(identity.session_id, None)
        await self._invalidate_hot_channel(
            identity,
            reason="supervisor_session_prepare_refresh",
        )
        release = await self._prepare_phase(
            "live_execution_release",
            self.protocol.release_live_session(
                self.endpoint,
                runtime_session_id=identity.session_id,
            ),
        )
        await self._prepare_phase("model_route", self.model_router.prepare(identity))
        hermes_profile_name = self._hermes_profile_name(identity.profile_id)
        hermes_provider = self.model_router.hermes_provider(identity)
        # Backend owns the authoritative Session row, while Runtime owns the
        # Hermes execution Session.  A newly-created Backend Session therefore
        # reaches Runtime before it exists in Dashboard.  The import endpoint
        # is idempotent (existing ids are returned as skipped), so materialize
        # the execution Session at the same singleflight Prepare boundary used
        # for both Web and internal turns.
        await self._prepare_phase(
            "session_import",
            self.protocol.create_session(
                self.endpoint,
                runtime_session_id=identity.session_id,
                model=identity.gateway_model_id,
                provider=hermes_provider,
                hermes_profile_name=hermes_profile_name,
            ),
        )
        route_prepare_proof = self.protocol.issue_route_prepare_proof(
            release,
            runtime_session_id=identity.session_id,
            hermes_profile_name=hermes_profile_name,
            expected_model=identity.gateway_model_id,
            expected_provider=hermes_provider,
        )
        await self._prepare_phase(
            "dashboard_prewarm",
            self.protocol.prewarm_session(
                self.endpoint,
                runtime_session_id=identity.session_id,
                hermes_profile_name=hermes_profile_name,
                runtime_agent_profile_id=self._profile_number(identity.profile_id),
                hot_channel_identity=self.hot_identity(identity),
                expected_provider=hermes_provider,
                route_prepare_proof=route_prepare_proof,
                # Hermes 0.20.1 eager resume transfers the supplied SessionDB
                # to the live Agent.  That ownership is safe for named Profile
                # handles, but default uses Dashboard's process-wide launch DB;
                # closing the Agent would poison every later resume.  Deferred
                # resume still completes inside this prewarm via wait_ready.
                eager_build=hermes_profile_name != "default",
            ),
        )
        self._route_prepare_proofs[identity.session_id] = (
            identity.digest,
            route_prepare_proof,
        )

    async def refresh_session_profile(self, identity: PrepareIdentity) -> None:
        await self._prepare_phase(
            "profile_prompt_refresh",
            self.protocol.refresh_session_profile(
                self.endpoint,
                runtime_session_id=identity.session_id,
                model=identity.gateway_model_id,
                provider=self.model_router.hermes_provider(identity),
                hermes_profile_name=self._hermes_profile_name(identity.profile_id),
            ),
        )

    @staticmethod
    async def _prepare_phase(
        phase: str,
        awaitable: Awaitable[object],
    ) -> object:
        try:
            return await awaitable
        except HermesProtocolError as exc:
            raise HermesSessionPrepareError(phase, exc) from exc

    async def stream(
        self,
        *,
        identity: PrepareIdentity,
        run_id: str,
        prompt: str,
        attachments: list[dict[str, object]] | tuple[dict[str, object], ...] = (),
        trusted_context: dict[str, object] | None = None,
        prompt_submit_started_sink: (
            Callable[[], Awaitable[None] | None] | None
        ) = None,
        prompt_ack_sink: Callable[[], Awaitable[None]] | None = None,
        runtime_provider_run_id_sink: (
            Callable[[str], Awaitable[None]] | None
        ) = None,
    ) -> AsyncIterator[PlatformStreamEvent]:
        protocol_stream = self.protocol.stream_session_chat(
            self.endpoint,
            runtime_session_id=identity.session_id,
            message=prompt,
            attachments=attachments,
            expected_model=identity.gateway_model_id,
            expected_provider=self.model_router.hermes_provider(identity),
            hermes_profile_name=self._hermes_profile_name(identity.profile_id),
            runtime_agent_profile_id=self._profile_number(identity.profile_id),
            runtime_run_id=run_id,
            hot_channel_identity=self.hot_identity(identity),
            route_prepare_proof=self._route_prepare_proof(identity),
            trusted_context=trusted_context,
            platform_contract_version=2,
            prompt_submit_started_sink=prompt_submit_started_sink,
            prompt_ack_sink=prompt_ack_sink,
            runtime_provider_run_id_sink=runtime_provider_run_id_sink,
        )
        try:
            async for event in protocol_stream:
                yield event
        finally:
            close_stream = getattr(protocol_stream, "aclose", None)
            if callable(close_stream):
                await close_stream()

    async def cancel(
        self,
        identity: PrepareIdentity,
        *,
        runtime_provider_run_id: str | None = None,
    ) -> None:
        if not runtime_provider_run_id:
            raise HermesProtocolError(
                "runtime dashboard live session id is unavailable"
            )
        await self.protocol.stop_run(
            self.endpoint,
            runtime_provider_run_id=runtime_provider_run_id,
        )

    async def interrupt_and_settle_redirect(
        self,
        *,
        runtime_run_id: str,
        stored_session_id: str,
        profile_name: str = "default",
        timeout_seconds: float | None = None,
    ) -> DashboardInterruptSettleResult:
        """Use the live one-reader peer, or a unique no-submit recovery peer."""

        timeout = 30.0 if timeout_seconds is None else timeout_seconds
        profile_name = self._hermes_profile_name(profile_name)
        peers = self.control_peers
        if peers is not None:
            connection_id = await peers.current_connection_id(runtime_run_id)
            if connection_id is not None:
                try:
                    return await peers.interrupt_and_settle_redirect(
                        runtime_run_id=runtime_run_id,
                        stored_session_id=stored_session_id,
                        profile_name=profile_name,
                        timeout_seconds=timeout,
                    )
                except HermesProtocolError:
                    # A stream may close between the lookup and the control
                    # lock.  Only fall back once Registry proves that the old
                    # single-reader owner has actually been revoked.
                    if await peers.current_connection_id(runtime_run_id) is not None:
                        raise
        return await self.protocol.interrupt_and_settle_redirect(
            self.endpoint,
            runtime_session_id=stored_session_id,
            hermes_profile_name=profile_name,
            timeout_seconds=timeout,
        )

    async def apply_config(
        self,
        *,
        config_revision: str,
        config_digest: str,
        reload_skills: bool,
        reload_mcp: bool,
    ) -> dict[str, object]:
        generation = self._stable_positive_int(config_revision) % 2_147_483_647
        return await self.protocol.apply_config(
            self.endpoint,
            generation=max(1, generation),
            expected_projection_hash=config_digest,
            reload_skills=reload_skills,
            reload_mcp=reload_mcp,
            invalidate_agents=True,
        )

    async def get_session_messages(
        self,
        *,
        session_id: str,
        profile_name: str = "default",
    ) -> dict[str, object]:
        return await self.protocol.get_session_messages(
            self.endpoint,
            runtime_session_id=session_id,
            hermes_profile_name=self._hermes_profile_name(profile_name),
        )

    async def list_sessions(
        self,
        *,
        limit: int,
        offset: int = 0,
    ) -> dict[str, object]:
        return await self.protocol.list_sessions(
            self.endpoint,
            limit=limit,
            offset=offset,
        )

    async def rename_session(
        self,
        *,
        session_id: str,
        title: str,
        profile_name: str = "default",
    ) -> dict[str, object]:
        return await self.protocol.rename_session(
            self.endpoint,
            runtime_session_id=session_id,
            title=title,
            hermes_profile_name=self._hermes_profile_name(profile_name),
        )

    async def delete_session(
        self,
        *,
        session_id: str,
        profile_name: str = "default",
    ) -> dict[str, object]:
        try:
            return await self.protocol.delete_session(
                self.endpoint,
                runtime_session_id=session_id,
                hermes_profile_name=self._hermes_profile_name(profile_name),
            )
        except HermesProtocolError as exc:
            if exc.status_code != 404:
                raise
            return {"deleted": False, "already_missing": True}

    async def close_session(self, identity: PrepareIdentity) -> None:
        self._route_prepare_proofs.pop(identity.session_id, None)
        await self._invalidate_hot_channel(
            identity,
            reason="supervisor_session_evicted",
        )
        await self.protocol.release_live_session(
            self.endpoint,
            runtime_session_id=identity.session_id,
        )
        await self.model_router.close_session(identity)

    def _route_prepare_proof(self, identity: PrepareIdentity) -> object | None:
        cached = self._route_prepare_proofs.get(identity.session_id)
        if cached is None or cached[0] != identity.digest:
            return None
        return cached[1]

    async def _invalidate_hot_channel(
        self,
        identity: PrepareIdentity,
        *,
        reason: str,
    ) -> None:
        await self.hot_channels.invalidate_session(
            self._stable_positive_int(identity.runtime_id),
            self._stable_positive_int(identity.profile_id),
            self.endpoint.runtime_workspace_id,
            identity.session_id,
            reason=reason,
        )

    async def close(self) -> None:
        await self.hot_channels.close()
        await self.model_router.close()

    def hot_identity(
        self,
        identity: PrepareIdentity,
    ) -> DashboardHotChannelIdentity:
        config_generation = int(identity.config_revision[:8], 16) if all(
            char in "0123456789abcdef"
            for char in identity.config_revision[:8].lower()
        ) else self._stable_positive_int(identity.config_revision)
        model_number = self._model_number(identity.model_id)
        return DashboardHotChannelIdentity(
            org_id=self._stable_positive_int(identity.runtime_id),
            user_id=self._stable_positive_int(identity.profile_id),
            runtime_workspace_id=self.endpoint.runtime_workspace_id,
            runtime_session_db_id=self._stable_positive_int(identity.session_id),
            runtime_session_id=identity.session_id,
            runtime_id=identity.runtime_id,
            gateway_model_id=identity.gateway_model_id,
            config_generation=config_generation,
            endpoint_digest=hashlib.sha256(
                self.endpoint.endpoint_base_url.encode("utf-8")
            ).hexdigest(),
            credential_digest=hashlib.sha256(
                self.endpoint.api_key.encode("utf-8")
            ).hexdigest(),
            workspace_path_digest=hashlib.sha256(
                str(self.settings.WORKSPACE_DIR).encode("utf-8")
            ).hexdigest(),
            effective_model_entry_id=model_number,
            model_policy_digest=hashlib.sha256(
                (
                    f"{identity.model_id}:{identity.gateway_model_id}:"
                    f"{identity.config_revision}"
                ).encode("utf-8")
            ).hexdigest(),
            coordination_scope_key=f"runtime:{identity.runtime_id}",
            independent_session=True,
            reuse_across_turns=(
                getattr(self.settings, "RUNTIME_MODE", "personal") == "personal"
                and identity.profile_id == "default"
            ),
            max_coordination_slots=self.settings.max_resident_sessions,
        )

    @staticmethod
    def _stable_positive_int(value: str) -> int:
        return int.from_bytes(
            hashlib.sha256(value.encode("utf-8")).digest()[:8],
            "big",
        ) or 1

    @classmethod
    def _model_number(cls, value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            return cls._stable_positive_int(value)
        return max(1, number)

    @staticmethod
    def _profile_number(value: str) -> int | None:
        if value in {"", "default"} or value.startswith("application-"):
            return None
        tail = value.rsplit("-", 1)[-1]
        try:
            return int(tail)
        except ValueError:
            return HermesExecutionAdapter._stable_positive_int(value)

    @staticmethod
    def _hermes_profile_name(value: str) -> str:
        return (
            "default"
            if value in {"", "default"} or value.startswith("application-")
            else value
        )
