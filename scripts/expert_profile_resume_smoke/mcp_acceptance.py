"""Acceptance policy for dual-MCP Run, isolation, and lifecycle evidence."""

from __future__ import annotations

import json
from typing import Any

from .contracts import CheckpointStore, RunEvidence
from .mcp_contracts import (
    McpIdentity,
    has_tool_search,
    rendered_run,
    search_results,
)


class McpAcceptance:
    def __init__(self, *, store: CheckpointStore) -> None:
        self.store = store
        self.failures: list[str] = []
        self.beta_initial_live_ids: list[str] = []

    def fail(self, code: str) -> None:
        if code not in self.failures:
            self.failures.append(code)

    def positive(
        self,
        label: str,
        run: RunEvidence,
        identity: McpIdentity,
    ) -> None:
        if run.error:
            self.fail(f"{label}-run-error:{run.error}")
        if not has_tool_search(run):
            self.fail(f"{label}-missing-tool-search")
        if identity.marker not in rendered_run(run):
            self.fail(f"{label}-missing-marker")

    def negative(
        self,
        label: str,
        run: RunEvidence,
        identity: McpIdentity,
        *,
        marker_delta: int,
    ) -> None:
        if run.error:
            self.fail(f"{label}-run-error:{run.error}")
        if not has_tool_search(run):
            self.fail(f"{label}-missing-tool-search")
        if identity.server_name in search_results(run):
            self.fail(f"{label}-foreign-tool-visible")
        if marker_delta:
            self.fail(f"{label}-foreign-marker-called:{marker_delta}")

    def completed_invalidate(
        self,
        label: str,
        result: dict[str, Any],
    ) -> None:
        if result.get("status") != "completed":
            self.fail(f"{label}-idle-invalidate-failed")

    def working_busy(self, label: str, result: dict[str, Any]) -> None:
        if (
            result.get("status") != "error"
            or "SESSION_BUSY" not in json.dumps(result, ensure_ascii=False)
        ):
            self.fail(f"{label}-working-invalidate-was-not-session-busy")

    def beta_live_stable(self, label: str, run: RunEvidence) -> None:
        if not self.beta_initial_live_ids or not run.provider_live_ids:
            self.fail(f"{label}-beta-live-id-missing")
        elif not set(self.beta_initial_live_ids).intersection(run.provider_live_ids):
            self.fail(f"{label}-beta-live-agent-changed")

    def no_global_reload(self) -> None:
        observed = {
            str(item.get("method"))
            for item in self.store.checkpoint.control_methods
        }
        overlap = observed & {"skills.reload", "reload.mcp"}
        if overlap:
            self.fail(f"global-reload-observed:{sorted(overlap)}")
        self.store.checkpoint.evidence["no_global_reload"] = {
            "passed": not overlap,
            "audited_methods": sorted(observed),
        }
