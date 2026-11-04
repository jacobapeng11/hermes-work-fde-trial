from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class RuntimeEndpoint:
    runtime_workspace_id: int
    runtime_id: str
    endpoint_base_url: str
    endpoint_host_header: str
    api_key: str
    timeout_seconds: float = 30.0
    coordination_scope_key: str | None = None

    @property
    def coordination_scope(self) -> int | str:
        """Process-local lock namespace for one logical Runtime owner.

        Personal Runtime workspace ids and Application Agent ids are both
        integers.  Keeping the wire workspace id separate from this namespace
        prevents unrelated owners with the same numeric id from sharing a
        Dashboard lock or cold-run counter.
        """

        explicit = str(self.coordination_scope_key or "").strip()
        if explicit:
            return explicit
        # Preserve the existing public manager call shape for Personal
        # Runtime endpoints. The manager normalizes this integer into the same
        # internal namespace used by DashboardHotChannelIdentity.
        return int(self.runtime_workspace_id)
