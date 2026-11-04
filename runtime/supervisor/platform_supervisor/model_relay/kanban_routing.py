from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
from pathlib import Path
import re
from collections.abc import Mapping
from typing import Any, Awaitable, Callable
from uuid import uuid4

import yaml

from platform_supervisor.model_relay.relay import (
    ActiveModelRoute,
    ModelRouteRegistry,
)
from platform_supervisor.settings import SupervisorSettings
from platform_supervisor.model_relay.context_budget import (
    context_aware_max_output_tokens,
)
from platform_supervisor.provider_routes import (
    ProviderRouteBindingIdentity,
    ProviderRouteCapsuleCodec,
    ProviderRouteMaterial,
)


_EXPERT_PROFILE = re.compile(r"expert-[1-9][0-9]*")


@dataclass(frozen=True, slots=True)
class KanbanWorkerModelIdentity:
    run_id: str
    task_id: str
    board: str
    profile_id: str
    model_id: str
    upstream_model_id: str
    runtime_capabilities: dict[str, str]
    max_output_tokens: int
    api_mode: str = "chat_completions"
    org_id: int = 0
    provider_route: dict[str, object] | None = None
    provider_route_digest: str | None = None
    context_length: int = 128_000

    def __post_init__(self) -> None:
        if (self.provider_route is None) != (self.provider_route_digest is None):
            raise ValueError("provider route and digest must be supplied together")
        if (
            self.provider_route is not None
            and self.provider_route.get("digest") != self.provider_route_digest
        ):
            raise ValueError("provider route digest does not match grant")


class KanbanWorkerModelRouter:
    """Own one fail-closed Model Relay lease per expert Profile."""

    def __init__(
        self,
        settings: SupervisorSettings,
        registry: ModelRouteRegistry,
        *,
        provider_route_codec: ProviderRouteCapsuleCodec | None = None,
        provider_route_control_keys: Mapping[int, bytes] | None = None,
        provider_route_binding: ProviderRouteBindingIdentity | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self._config_lock = asyncio.Lock()
        self._tokens: set[str] = set()
        self._provider_route_codec = provider_route_codec
        self._provider_route_control_keys = dict(
            provider_route_control_keys or {}
        )
        self._provider_route_binding = provider_route_binding
        self._profile_config_reader: Callable[[str], Awaitable[dict[str, Any]]] | None = None
        self._profile_config_writer: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None

    def bind_profile_config_io(
        self,
        *,
        reader: Callable[[str], Awaitable[dict[str, Any]]],
        writer: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Use Hermes Dashboard as the authority for private Profile files."""
        self._profile_config_reader = reader
        self._profile_config_writer = writer

    def route_token(self, profile_id: str) -> str:
        self._validate_profile(profile_id)
        payload = "\0".join((self.settings.RUNTIME_ID, profile_id)).encode(
            "utf-8"
        )
        return hmac.new(
            self.settings.API_SERVER_KEY.get_secret_value().encode("utf-8"),
            b"model-relay-kanban-worker-v1\0" + payload,
            hashlib.sha256,
        ).hexdigest()

    def relay_base_url(self, profile_id: str) -> str:
        return (
            f"http://{self.settings.MODEL_RELAY_HOST}:"
            f"{self.settings.MODEL_RELAY_PORT}/model-relay/"
            f"{self.route_token(profile_id)}/v1"
        )

    async def activate(
        self,
        identity: KanbanWorkerModelIdentity,
    ) -> dict[str, str]:
        token = self.route_token(identity.profile_id)
        provider_route = self._open_provider_route(identity)
        max_output_tokens = context_aware_max_output_tokens(
            identity.max_output_tokens,
            identity.context_length,
        )
        route = ActiveModelRoute(
            route_token=token,
            run_id=identity.run_id,
            session_id=f"kanban:{identity.board}:{identity.task_id}",
            model_id=identity.model_id,
            upstream_model_id=identity.upstream_model_id,
            runtime_model_id=identity.upstream_model_id,
            runtime_capabilities=identity.runtime_capabilities,
            api_mode=identity.api_mode,
            max_output_tokens=max_output_tokens,
            context_length=identity.context_length,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=self.settings.MODEL_ROUTE_TTL_SECONDS),
            authorization_scope="kanban_worker",
            provider_base_url=(
                provider_route.provider_base_url if provider_route else None
            ),
            provider_api_key=(
                provider_route.provider_api_key if provider_route else None
            ),
        )
        await self.registry.register_kanban_worker(route)
        try:
            await self._configure_provider(identity)
        except BaseException:
            await self.registry.revoke_if_run(token, identity.run_id)
            raise
        self._tokens.add(token)
        return {"route_token": token, "run_id": identity.run_id}

    def _open_provider_route(
        self,
        identity: KanbanWorkerModelIdentity,
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
            material.upstream_model_id != identity.upstream_model_id
            or material.provider_route_digest != identity.provider_route_digest
        ):
            raise RuntimeError("PROVIDER_ROUTE_IDENTITY_MISMATCH")
        return material

    async def revoke(self, identity: KanbanWorkerModelIdentity) -> bool:
        return await self.registry.revoke_if_run(
            self.route_token(identity.profile_id),
            identity.run_id,
        )

    async def close(self) -> None:
        for token in tuple(self._tokens):
            await self.registry.revoke(token)
        self._tokens.clear()

    async def _configure_provider(
        self,
        identity: KanbanWorkerModelIdentity,
    ) -> None:
        path = (
            self.settings.HERMES_HOME
            / "profiles"
            / identity.profile_id
            / "config.yaml"
        )
        base_url = self.relay_base_url(identity.profile_id)
        max_output_tokens = context_aware_max_output_tokens(
            identity.max_output_tokens,
            identity.context_length,
        )
        desired: dict[str, Any] = {
            "name": "platform",
            "base_url": base_url,
            "api": base_url,
            "key_env": "OPENAI_API_KEY",
            "transport": identity.api_mode,
            "default_model": identity.upstream_model_id,
            "max_output_tokens": max_output_tokens,
            "discover_models": False,
            "models": {
                identity.upstream_model_id: {
                    "context_length": identity.context_length,
                    "supports_vision": (
                        identity.runtime_capabilities.get("vision") == "native"
                    ),
                }
            },
        }
        if identity.runtime_capabilities.get("thinking_mode") == "enabled":
            desired["extra_body"] = {"thinking": {"type": "enabled"}}
        async with self._config_lock:
            if self._profile_config_reader is not None:
                document = await self._profile_config_reader(identity.profile_id)
            else:
                document = self._read_document(path)
            providers = document.setdefault("providers", {})
            model = document.setdefault("model", {})
            if not isinstance(providers, dict) or not isinstance(model, dict):
                raise RuntimeError(
                    f"Hermes model routing config is invalid: {path}"
                )
            providers["platform"] = desired
            model["provider"] = "custom:platform"
            model["default"] = identity.upstream_model_id
            model["base_url"] = base_url
            model.pop("default_model_entry_id", None)
            if self._profile_config_writer is not None:
                await self._profile_config_writer(identity.profile_id, document)
            else:
                self._write_document(path, document)

    @staticmethod
    def _validate_profile(profile_id: str) -> None:
        if _EXPERT_PROFILE.fullmatch(profile_id) is None:
            raise ValueError("kanban worker requires an expert Profile")

    @staticmethod
    def _read_document(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise RuntimeError(f"Hermes config is unavailable: {path}")
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise RuntimeError(f"Hermes config root is invalid: {path}")
        return parsed

    @staticmethod
    def _write_document(path: Path, document: dict[str, Any]) -> None:
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
