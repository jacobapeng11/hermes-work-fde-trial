"""Two registered MCP connections and their Profile binding projections."""

from __future__ import annotations

import hashlib
from typing import Any

from .contracts import SmokeError
from .mcp_contracts import McpIdentity
from .profile_fixture import ProfileFixture


class McpProfileFixture(ProfileFixture):
    def identity(self, connection: str) -> McpIdentity:
        if connection not in {"a", "b"}:
            raise ValueError("invalid MCP connection")
        suffix = self.store.checkpoint.suffix
        return McpIdentity(
            connection=connection,
            server_name=f"isolation-mcp-{connection}-{suffix}",
            marker_tool=self.marker.isolation_tool_name(connection),
            marker=f"MCP_{connection.upper()}_{suffix}",
            hold_tool=(
                self.marker.isolation_tool_name("a", "hold")
                if connection == "a"
                else None
            ),
        )

    async def create_pair(self) -> tuple[dict[str, Any], dict[str, Any]]:
        alpha = await self.create_expert("mcp-alpha")
        beta = await self.create_expert("mcp-beta")
        self.experts["alpha"] = alpha
        self.experts["beta"] = beta
        return alpha, beta

    async def create_registry_keeper(self) -> dict[str, Any]:
        keeper = await self.create_expert("mcp-a-registry-keeper")
        self.experts["keeper"] = keeper
        return keeper

    async def bind(self, profile: str, connection: str) -> dict[str, Any]:
        expert = self.experts[profile]
        profile_name = str(expert["hermes_profile_name"])
        identity = self.identity(connection)
        self.store.record_control(
            "profile.mcp.replace",
            profile=profile_name,
            connection=connection,
        )
        await self.profiles.replace_mcp_servers(
            self.target.endpoint,
            name=profile_name,
            servers={
                identity.server_name: {
                    "transport": "streamable_http",
                    "enabled": True,
                    "url": self.marker.url("registry", connection),
                }
            },
        )
        observed = await self.dashboard.list_profile_mcp_servers(
            self.target.endpoint,
            profile_name=profile_name,
        )
        names = {str(item.get("name") or "") for item in observed}
        if names != {identity.server_name}:
            raise SmokeError(
                f"{profile} Profile MCP binding did not round-trip exactly"
            )
        return {
            "profile": profile_name,
            "runtime_agent_profile_id": int(expert["runtime_agent_profile_id"]),
            "connection": connection,
            "server_name": identity.server_name,
            "url_sha256": hashlib.sha256(
                self.marker.url("registry", connection).encode("utf-8")
            ).hexdigest(),
            "enabled": all(item.get("enabled") is not False for item in observed),
        }

    def profile_id(self, profile: str) -> int:
        return int(self.experts[profile]["runtime_agent_profile_id"])
