from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
from pathlib import Path
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import yaml

from platform_supervisor.model_relay.relay import ActiveModelRoute, ModelRouteRegistry
from platform_supervisor.model_relay.context_budget import (
    context_aware_max_output_tokens,
)
from platform_supervisor.provider_routes import (
    ProviderRouteBindingIdentity,
    ProviderRouteCapsuleCodec,
    ProviderRouteMaterial,
)
from platform_supervisor.settings import SupervisorSettings

if TYPE_CHECKING:
    from platform_supervisor.execution.sessions import PrepareIdentity


def _is_root_profile(profile_id: str) -> bool:
    """Application runtimes keep an isolated root config, not an expert profile."""

    return profile_id in {"", "default"} or profile_id.startswith("application-")


class SessionModelRouter:
    """Own Hermes' per-Session localhost provider and active Run binding."""

    def __init__(
        self,
        settings: SupervisorSettings,
        registry: ModelRouteRegistry,
        *,
        provider_route_codec: ProviderRouteCapsuleCodec | None = None,
        provider_route_control_keys: Mapping[int, bytes] | None = None,
        provider_route_binding: ProviderRouteBindingIdentity | None = None,
        route_renewal_interval_seconds: float | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self._config_lock = asyncio.Lock()
        self._session_providers: dict[str, tuple[str, str]] = {}
        self._profile_config_reader: Callable[
            [str], Awaitable[dict[str, Any]]
        ] | None = None
        self._profile_config_writer: Callable[
            [str, dict[str, Any]], Awaitable[None]
        ] | None = None
        self._provider_route_warmup: Callable[[str, str], None] | None = None
        self._provider_route_codec = provider_route_codec
        self._provider_route_control_keys = dict(
            provider_route_control_keys or {}
        )
        self._provider_route_binding = provider_route_binding
        self._session_provider_routes: dict[str, ProviderRouteMaterial] = {}
        self._post_turn_routes: dict[
            str,
            tuple["PrepareIdentity", str, str],
        ] = {}
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._renewal_tasks: dict[str, asyncio.Task[None]] = {}
        self._route_renewal_interval = (
            route_renewal_interval_seconds
            if route_renewal_interval_seconds is not None
            else max(1.0, settings.MODEL_ROUTE_TTL_SECONDS / 3)
        )

    def bind_profile_config_io(
        self,
        *,
        reader: Callable[[str], Awaitable[dict[str, Any]]],
        writer: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Route Hermes config mutations through its official Dashboard API."""
        self._profile_config_reader = reader
        self._profile_config_writer = writer

    def bind_provider_route_warmup(
        self,
        callback: Callable[[str, str], None],
    ) -> None:
        """Start transport warmup after a validated Provider route activates."""
        self._provider_route_warmup = callback

    def route_token(self, identity: PrepareIdentity) -> str:
        payload = "\0".join(
            (
                identity.runtime_id,
                identity.session_id,
                identity.profile_id,
                identity.model_id,
                identity.config_revision,
            )
        ).encode("utf-8")
        return hmac.new(
            self.settings.API_SERVER_KEY.get_secret_value().encode("utf-8"),
            b"model-relay-session-v1\0" + payload,
            hashlib.sha256,
        ).hexdigest()

    def provider_name(self, identity: PrepareIdentity) -> str:
        return f"runtime-{self.route_token(identity)[:32]}"

    def post_turn_route_token(
        self,
        identity: PrepareIdentity,
        run_id: str,
    ) -> str:
        payload = "\0".join(
            (
                identity.runtime_id,
                identity.session_id,
                identity.profile_id,
                identity.model_id,
                identity.config_revision,
                run_id,
            )
        ).encode("utf-8")
        return hmac.new(
            self.settings.API_SERVER_KEY.get_secret_value().encode("utf-8"),
            b"model-relay-post-turn-v1\0" + payload,
            hashlib.sha256,
        ).hexdigest()

    def post_turn_provider_name(
        self,
        identity: PrepareIdentity,
        run_id: str,
    ) -> str:
        return (
            "runtime-review-"
            f"{self.post_turn_route_token(identity, run_id)[:24]}"
        )

    def hermes_provider(self, identity: PrepareIdentity) -> str:
        return f"custom:{self.provider_name(identity)}"

    def relay_base_url(self, identity: PrepareIdentity) -> str:
        return (
            f"http://{self.settings.MODEL_RELAY_HOST}:"
            f"{self.settings.MODEL_RELAY_PORT}/model-relay/"
            f"{self.route_token(identity)}/v1"
        )

    async def prepare(self, identity: PrepareIdentity) -> None:
        provider_route = self._open_provider_route(identity)
        # The route has been fully validated/decrypted at this point.  Start
        # establishing its transport before the profile document write and
        # registry update so those control-plane awaits overlap the network
        # warmup instead of delaying it.
        if provider_route is not None and self._provider_route_warmup is not None:
            self._provider_route_warmup(
                provider_route.provider_base_url,
                provider_route.provider_api_key.get_secret_value(),
            )
        provider_name = self.provider_name(identity)
        max_output_tokens = context_aware_max_output_tokens(
            identity.max_output_tokens,
            identity.context_length,
        )
        async with self._config_lock:
            document = await self._read_profile_document(identity)
            providers = document.setdefault("providers", {})
            if not isinstance(providers, dict):
                raise RuntimeError("Hermes providers config is invalid")
            supports_native_vision = (
                (identity.runtime_capabilities or {}).get("vision")
                == "native"
            )
            config_changed = False
            if self.settings.RUNTIME_MODE == "application":
                # Application Runtime serves one configured model. Hermes'
                # image router reads this official top-level capability hint;
                # without it a custom relay provider can fall back to a text
                # pre-analysis pass even when the signed route is native-vision.
                model_config = document.setdefault("model", {})
                if not isinstance(model_config, dict):
                    raise RuntimeError("Hermes model config is invalid")
                if model_config.get("supports_vision") != (
                    supports_native_vision
                ):
                    model_config["supports_vision"] = supports_native_vision
                    config_changed = True
            elif supports_native_vision:
                agent_config = document.setdefault("agent", {})
                if not isinstance(agent_config, dict):
                    raise RuntimeError("Hermes agent config is invalid")
                if agent_config.get("image_input_mode") != "native":
                    agent_config["image_input_mode"] = "native"
                    config_changed = True
                custom_provider = providers.setdefault("custom", {})
                if not isinstance(custom_provider, dict):
                    raise RuntimeError("Hermes custom provider config is invalid")
                custom_models = custom_provider.setdefault("models", {})
                if not isinstance(custom_models, dict):
                    raise RuntimeError("Hermes custom provider models config is invalid")
                custom_model = custom_models.setdefault(
                    identity.gateway_model_id,
                    {},
                )
                if not isinstance(custom_model, dict):
                    raise RuntimeError("Hermes custom model config is invalid")
                if custom_model.get("supports_vision") is not True:
                    custom_model["supports_vision"] = True
                    config_changed = True
            desired = {
                "name": provider_name,
                "base_url": self.relay_base_url(identity),
                "api": self.relay_base_url(identity),
                "key_env": "OPENAI_API_KEY",
                "transport": identity.api_mode,
                "default_model": identity.gateway_model_id,
                "max_output_tokens": max_output_tokens,
                "discover_models": False,
                "models": {
                    identity.gateway_model_id: {
                        "context_length": identity.context_length,
                        "supports_vision": supports_native_vision,
                    }
                },
            }
            # Hermes forwards custom-provider ``extra_body`` verbatim.  This
            # activates the upstream thinking mode without changing Hermes'
            # provider implementation; the capability contract stays platform-owned.
            if identity.runtime_capabilities and (
                identity.runtime_capabilities.get("thinking_mode") == "enabled"
            ):
                desired["extra_body"] = {"thinking": {"type": "enabled"}}
            if providers.get(provider_name) != desired or config_changed:
                providers[provider_name] = desired
                await self._write_profile_document(identity, document)
            self._session_providers[identity.session_id] = (
                identity.profile_id,
                provider_name,
            )
            if provider_route is None:
                self._session_provider_routes.pop(identity.session_id, None)
            else:
                self._session_provider_routes[identity.session_id] = provider_route
            await self.registry.register_model_listing(
                self.route_token(identity), identity.gateway_model_id
            )

    async def activate(self, identity: PrepareIdentity, run_id: str) -> None:
        now = datetime.now(timezone.utc)
        provider_route = self._provider_route_for_activation(identity)
        provider_base_url = (
            provider_route.provider_base_url if provider_route else None
        )
        provider_api_key = (
            provider_route.provider_api_key if provider_route else None
        )
        max_output_tokens = context_aware_max_output_tokens(
            identity.max_output_tokens,
            identity.context_length,
        )
        try:
            await self.registry.register(
                ActiveModelRoute(
                    route_token=self.route_token(identity),
                    run_id=run_id,
                    session_id=identity.session_id,
                    model_id=identity.model_id,
                    upstream_model_id=identity.gateway_model_id,
                    runtime_model_id=identity.gateway_model_id,
                    runtime_capabilities=identity.runtime_capabilities or {},
                    api_mode=identity.api_mode,
                    max_output_tokens=max_output_tokens,
                    context_length=identity.context_length,
                    expires_at=now
                    + timedelta(
                        seconds=self.settings.MODEL_ROUTE_TTL_SECONDS
                    ),
                    provider_base_url=provider_base_url,
                    provider_api_key=provider_api_key,
                )
            )
            self._start_route_renewal(run_id, self.route_token(identity))
            if self.settings.RUNTIME_MODE != "personal":
                return
            route_token = self.post_turn_route_token(identity, run_id)
            provider_name = self.post_turn_provider_name(identity, run_id)
            await self.registry.register(
                ActiveModelRoute(
                    route_token=route_token,
                    run_id=run_id,
                    session_id=identity.session_id,
                    model_id=identity.model_id,
                    upstream_model_id=identity.gateway_model_id,
                    runtime_model_id=identity.gateway_model_id,
                    runtime_capabilities=identity.runtime_capabilities or {},
                    api_mode=identity.api_mode,
                    max_output_tokens=min(
                        max_output_tokens,
                        self.settings.POST_TURN_REVIEW_MAX_OUTPUT_TOKENS,
                    ),
                    context_length=identity.context_length,
                    expires_at=now
                    + timedelta(
                        seconds=self.settings.MODEL_ROUTE_TTL_SECONDS
                    ),
                    authorization_scope="post_turn_review",
                    idle_timeout_seconds=(
                        self.settings.POST_TURN_REVIEW_IDLE_TIMEOUT_SECONDS
                    ),
                    provider_base_url=provider_base_url,
                    provider_api_key=provider_api_key,
                ),
                pending=True,
            )
            await self._configure_post_turn_provider(
                identity=identity,
                route_token=route_token,
                provider_name=provider_name,
            )
            self._post_turn_routes[run_id] = (
                identity,
                route_token,
                provider_name,
            )
        except BaseException:
            await self.registry.revoke_run(run_id)
            raise

    async def revoke_run(self, run_id: str) -> None:
        await self._cancel_route_renewal(run_id)
        await self.registry.revoke_run(run_id)
        await self._remove_post_turn_provider(run_id)

    async def finish_run(
        self,
        identity: PrepareIdentity,
        run_id: str,
        *,
        completed: bool,
    ) -> None:
        del identity
        await self._cancel_route_renewal(run_id)
        now = datetime.now(timezone.utc)
        await self.registry.complete_run(
            run_id,
            completed=completed,
            activated_at=now,
            activation_window_seconds=(
                self.settings.POST_TURN_REVIEW_ACTIVATION_WINDOW_SECONDS
            ),
            max_duration_seconds=(
                self.settings.POST_TURN_REVIEW_MAX_DURATION_SECONDS
            ),
        )
        if not completed:
            await self._remove_post_turn_provider(run_id)
            return
        task = asyncio.create_task(
            self._cleanup_post_turn_after_expiry(run_id),
            name=f"model-route-post-turn-cleanup:{run_id}",
        )
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def close_session(self, identity: PrepareIdentity) -> None:
        await self.registry.revoke(self.route_token(identity))
        self._session_provider_routes.pop(identity.session_id, None)
        run_ids = [
            run_id
            for run_id, (candidate, _token, _provider) in (
                self._post_turn_routes.items()
            )
            if candidate.session_id == identity.session_id
        ]
        for run_id in run_ids:
            await self._cancel_route_renewal(run_id)
            await self.registry.revoke_run(run_id)
            await self._remove_post_turn_provider(run_id)
        async with self._config_lock:
            current = self._session_providers.get(identity.session_id)
            if current is None or current[1] != self.provider_name(identity):
                return
            profile_id, provider_name = current
            document = await self._read_profile_document(identity)
            providers = document.get("providers")
            if (
                isinstance(providers, dict)
                and providers.pop(provider_name, None) is not None
            ):
                if not providers:
                    document.pop("providers", None)
                await self._write_profile_document(identity, document)
            self._session_providers.pop(identity.session_id, None)

    async def close(self) -> None:
        renewal_tasks = tuple(self._renewal_tasks.values())
        for task in renewal_tasks:
            task.cancel()
        if renewal_tasks:
            await asyncio.gather(*renewal_tasks, return_exceptions=True)
        self._renewal_tasks.clear()
        tasks = tuple(self._cleanup_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._cleanup_tasks.clear()
        for run_id in tuple(self._post_turn_routes):
            await self.registry.revoke_run(run_id)
            await self._remove_post_turn_provider(run_id)
        self._session_provider_routes.clear()

    def _start_route_renewal(self, run_id: str, route_token: str) -> None:
        existing = self._renewal_tasks.get(run_id)
        if existing is not None and not existing.done():
            existing.cancel()
        task = asyncio.create_task(
            self._renew_active_run_route(run_id, route_token),
            name=f"model-route-renew:{run_id}",
        )
        self._renewal_tasks[run_id] = task

        def _discard(finished: asyncio.Task[None]) -> None:
            if self._renewal_tasks.get(run_id) is finished:
                self._renewal_tasks.pop(run_id, None)

        task.add_done_callback(_discard)

    async def _renew_active_run_route(
        self,
        run_id: str,
        route_token: str,
    ) -> None:
        while True:
            await asyncio.sleep(self._route_renewal_interval)
            renewed = await self.registry.renew_active_run_route(
                route_token,
                run_id,
                ttl_seconds=self.settings.MODEL_ROUTE_TTL_SECONDS,
            )
            if not renewed:
                return

    async def _cancel_route_renewal(self, run_id: str) -> None:
        task = self._renewal_tasks.pop(run_id, None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _open_provider_route(
        self,
        identity: PrepareIdentity,
    ) -> ProviderRouteMaterial | None:
        if identity.provider_route is None:
            if self.settings.PROVIDER_ROUTE_ACCEPTANCE_MODE == "required":
                raise RuntimeError("PROVIDER_ROUTE_REQUIRED")
            return None
        if (
            self._provider_route_codec is None
            or not self._provider_route_control_keys
            or self._provider_route_binding is None
        ):
            raise RuntimeError("PROVIDER_ROUTE_CONTROL_UNAVAILABLE")
        material = self._provider_route_codec.open(
            grant=identity.provider_route,
            control_root_keys=self._provider_route_control_keys,
            binding=self._provider_route_binding,
            expected_org_id=identity.org_id,
            expected_model_entry_id=identity.model_id,
        )
        if (
            material.upstream_model_id != identity.gateway_model_id
            or material.provider_route_digest != identity.provider_route_digest
        ):
            raise RuntimeError("PROVIDER_ROUTE_IDENTITY_MISMATCH")
        return material

    def _provider_route_for_activation(
        self,
        identity: PrepareIdentity,
    ) -> ProviderRouteMaterial | None:
        material = self._session_provider_routes.get(identity.session_id)
        if identity.provider_route is not None and material is None:
            raise RuntimeError("PROVIDER_ROUTE_NOT_PREPARED")
        if (
            material is not None
            and material.provider_route_digest != identity.provider_route_digest
        ):
            raise RuntimeError("PROVIDER_ROUTE_IDENTITY_MISMATCH")
        if (
            material is None
            and self.settings.PROVIDER_ROUTE_ACCEPTANCE_MODE == "required"
        ):
            raise RuntimeError("PROVIDER_ROUTE_REQUIRED")
        return material

    async def _configure_post_turn_provider(
        self,
        *,
        identity: PrepareIdentity,
        route_token: str,
        provider_name: str,
    ) -> None:
        base_url = (
            f"http://{self.settings.MODEL_RELAY_HOST}:"
            f"{self.settings.MODEL_RELAY_PORT}/model-relay/"
            f"{route_token}/v1"
        )
        max_output_tokens = context_aware_max_output_tokens(
            identity.max_output_tokens,
            identity.context_length,
        )
        desired = {
            "name": provider_name,
            "base_url": base_url,
            "api": base_url,
            "key_env": "OPENAI_API_KEY",
            "transport": identity.api_mode,
            "default_model": identity.gateway_model_id,
            "max_output_tokens": min(
                max_output_tokens,
                self.settings.POST_TURN_REVIEW_MAX_OUTPUT_TOKENS,
            ),
            "discover_models": False,
            "models": {
                identity.gateway_model_id: {
                    "context_length": identity.context_length,
                        "supports_vision": (
                            (identity.runtime_capabilities or {}).get("vision")
                            == "native"
                        ),
                }
            },
        }
        async with self._config_lock:
            document = await self._read_profile_document(identity)
            providers = document.setdefault("providers", {})
            auxiliary = document.setdefault("auxiliary", {})
            if not isinstance(providers, dict) or not isinstance(
                auxiliary,
                dict,
            ):
                raise RuntimeError(
                    "Hermes model routing config is invalid"
                )
            providers[provider_name] = desired
            auxiliary["background_review"] = {
                "provider": f"custom:{provider_name}",
                "model": identity.gateway_model_id,
            }
            await self._write_profile_document(identity, document)

    async def _cleanup_post_turn_after_expiry(self, run_id: str) -> None:
        await asyncio.sleep(
            self.settings.POST_TURN_REVIEW_MAX_DURATION_SECONDS
        )
        await self.registry.revoke_run(run_id)
        await self._remove_post_turn_provider(run_id)

    async def _remove_post_turn_provider(self, run_id: str) -> None:
        record = self._post_turn_routes.pop(run_id, None)
        if record is None:
            return
        identity, _route_token, provider_name = record
        async with self._config_lock:
            document = await self._read_profile_document(identity)
            changed = False
            providers = document.get("providers")
            if (
                isinstance(providers, dict)
                and providers.pop(provider_name, None) is not None
            ):
                if not providers:
                    document.pop("providers", None)
                changed = True
            auxiliary = document.get("auxiliary")
            background_review = (
                auxiliary.get("background_review")
                if isinstance(auxiliary, dict)
                else None
            )
            if (
                isinstance(background_review, dict)
                and background_review.get("provider")
                == f"custom:{provider_name}"
            ):
                auxiliary["background_review"] = {
                    "provider": "auto",
                    "model": "",
                }
                changed = True
            if changed:
                await self._write_profile_document(identity, document)

    async def _read_profile_document(
        self,
        identity: PrepareIdentity,
    ) -> dict[str, Any]:
        if (
            not _is_root_profile(identity.profile_id)
            and self._profile_config_reader is not None
        ):
            document = await self._profile_config_reader(identity.profile_id)
            if not isinstance(document, dict):
                raise RuntimeError("Hermes config root is invalid")
            return document
        return self._read_document(self._config_path(identity))

    async def _write_profile_document(
        self,
        identity: PrepareIdentity,
        document: dict[str, Any],
    ) -> None:
        if (
            not _is_root_profile(identity.profile_id)
            and self._profile_config_writer is not None
        ):
            await self._profile_config_writer(identity.profile_id, document)
            return
        self._write_document(self._config_path(identity), document)

    def _config_path(self, identity: PrepareIdentity) -> Path:
        if _is_root_profile(identity.profile_id):
            return self.settings.HERMES_HOME / "config.yaml"
        return (
            self.settings.HERMES_HOME
            / "profiles"
            / self._profile_directory(identity.profile_id)
            / "config.yaml"
        )

    @staticmethod
    def _profile_directory(profile_id: str) -> str:
        normalized = "".join(
            character
            for character in profile_id
            if character.isalnum() or character in {"-", "_"}
        )
        if not normalized or normalized != profile_id:
            raise RuntimeError("Hermes profile identifier is not path-safe")
        return normalized
    @staticmethod
    def _read_document(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise RuntimeError(f"Hermes config is unavailable: {path}")
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise RuntimeError(f"Hermes config root is invalid: {path}")
        return parsed

    @staticmethod
    def _write_document(path: Path, document: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        payload = yaml.safe_dump(
            document,
            sort_keys=False,
            allow_unicode=True,
        )
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
