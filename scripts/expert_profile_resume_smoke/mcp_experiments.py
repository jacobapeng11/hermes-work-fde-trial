"""M0-M3 dual-MCP orchestration without transport or database dependencies."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from .contracts import CheckpointStore, Target
from .marker_server import MarkerMcp
from .mcp_acceptance import McpAcceptance
from .mcp_contracts import marker_count, marker_snapshot
from .mcp_fixture import McpProfileFixture
from .mcp_prompts import McpPrompts
from .runtime_edge import RuntimeEdge


class McpExperiments:
    def __init__(
        self,
        *,
        target: Target,
        baseline: dict[str, Any],
        store: CheckpointStore,
        marker: MarkerMcp,
        profiles: McpProfileFixture,
        edge: RuntimeEdge,
    ) -> None:
        self.target = target
        self.baseline = baseline
        self.store = store
        self.marker = marker
        self.profiles = profiles
        self.edge = edge
        self.acceptance = McpAcceptance(store=store)
        self.alpha_session: dict[str, Any] = {}
        self.beta_session: dict[str, Any] = {}
        self.default_session: dict[str, Any] = {}
        self.keeper_session: dict[str, Any] = {}
        self.keeper_live_ids: list[str] = []

    @property
    def suffix(self) -> str:
        return self.store.checkpoint.suffix

    def save(self, name: str, evidence: dict[str, Any]) -> None:
        self.store.checkpoint.evidence[name] = evidence
        self.store.save()

    async def execute(self) -> dict[str, Any]:
        alpha, beta = await self.profiles.create_pair()
        projection = {
            "alpha": await self.profiles.bind("alpha", "a"),
            "beta": await self.profiles.bind("beta", "b"),
        }
        self.alpha_session = await self.edge.create_session(
            int(alpha["runtime_agent_profile_id"]), "mcp-alpha"
        )
        self.beta_session = await self.edge.create_session(
            int(beta["runtime_agent_profile_id"]), "mcp-beta"
        )
        self.default_session = await self.edge.create_session(None, "mcp-default")
        registry_ready = await self._m0_m1(projection)
        if registry_ready:
            await self._pin_a_registry()
            await self._m2()
            await self._m3()
        else:
            self.save(
                "blocked",
                {
                    "reason": "blocked_by_registry",
                    "skipped": ["M2", "M3"],
                },
            )
        self._assert_no_global_reload()
        return self.summarize()

    async def _m0_m1(self, projection: dict[str, Any]) -> bool:
        a = self.profiles.identity("a")
        b = self.profiles.identity("b")
        alpha_positive = await self.edge.run(
            self.alpha_session,
            McpPrompts.positive(a),
            "M1-alpha-positive",
        )
        beta_positive = await self.edge.run(
            self.beta_session,
            McpPrompts.positive(b),
            "M1-beta-positive",
        )
        self.acceptance.beta_initial_live_ids = list(
            beta_positive.provider_live_ids
        )
        ready_snapshot = marker_snapshot(self.marker.history)
        registry_ready = (
            ready_snapshot["initialize_a"] > 0
            and ready_snapshot["initialize_b"] > 0
        )
        if not registry_ready:
            self.acceptance.fail("blocked_by_registry")
        self.save(
            "M0",
            {
                "projection": projection,
                "registry_snapshot": ready_snapshot,
                "registry_ready": registry_ready,
            },
        )

        before_alpha_negative = marker_count(
            self.marker.history, connection="b", event="tool_call"
        )
        alpha_negative = await self.edge.run(
            self.alpha_session,
            McpPrompts.negative(b),
            "M1-alpha-negative-b",
        )
        alpha_cross_delta = marker_count(
            self.marker.history, connection="b", event="tool_call"
        ) - before_alpha_negative

        before_beta_negative = marker_count(
            self.marker.history, connection="a", event="tool_call"
        )
        beta_negative = await self.edge.run(
            self.beta_session,
            McpPrompts.negative(a),
            "M1-beta-negative-a",
        )
        beta_cross_delta = marker_count(
            self.marker.history, connection="a", event="tool_call"
        ) - before_beta_negative

        before_default = marker_snapshot(self.marker.history)
        default_negative = await self.edge.run(
            self.default_session,
            McpPrompts.negative(a, b),
            "M1-default-negative",
        )
        after_default = marker_snapshot(self.marker.history)
        default_call_delta = {
            connection: (
                after_default[f"tool_call_{connection}"]
                - before_default[f"tool_call_{connection}"]
            )
            for connection in ("a", "b")
        }
        self.acceptance.positive("M1-alpha", alpha_positive, a)
        self.acceptance.positive("M1-beta", beta_positive, b)
        self.acceptance.negative(
            "M1-alpha-negative",
            alpha_negative,
            b,
            marker_delta=alpha_cross_delta,
        )
        self.acceptance.negative(
            "M1-beta-negative",
            beta_negative,
            a,
            marker_delta=beta_cross_delta,
        )
        self.acceptance.negative(
            "M1-default-a",
            default_negative,
            a,
            marker_delta=default_call_delta["a"],
        )
        self.acceptance.negative(
            "M1-default-b",
            default_negative,
            b,
            marker_delta=default_call_delta["b"],
        )
        self.save(
            "M1",
            {
                "alpha_positive": asdict(alpha_positive),
                "beta_positive": asdict(beta_positive),
                "alpha_negative": asdict(alpha_negative),
                "beta_negative": asdict(beta_negative),
                "default_negative": asdict(default_negative),
                "cross_marker_delta": {
                    "alpha_to_b": alpha_cross_delta,
                    "beta_to_a": beta_cross_delta,
                    "default": default_call_delta,
                },
            },
        )
        return registry_ready

    async def _pin_a_registry(self) -> None:
        a = self.profiles.identity("a")
        keeper = await self.profiles.create_registry_keeper()
        projection = await self.profiles.bind("keeper", "a")
        self.keeper_session = await self.edge.create_session(
            int(keeper["runtime_agent_profile_id"]), "mcp-a-registry-keeper"
        )
        run = await self.edge.run(
            self.keeper_session,
            McpPrompts.positive(a),
            "M0-a-registry-keeper",
        )
        self.keeper_live_ids = list(run.provider_live_ids)
        self.acceptance.positive("M0-keeper", run, a)
        self.save(
            "registry_keeper",
            {
                "projection": projection,
                "run": asdict(run),
                "purpose": "keep mcp-a registered while Alpha binds mcp-b",
            },
        )

    async def _m2(self) -> None:
        a = self.profiles.identity("a")
        b = self.profiles.identity("b")
        projection = await self.profiles.bind("alpha", "b")
        invalidation = await self.edge.invalidate(
            self.alpha_session, "M2-alpha-idle-invalidate"
        )
        before_alpha = marker_snapshot(self.marker.history)
        alpha = await self.edge.run(
            self.alpha_session,
            McpPrompts.positive(b, negative=a),
            "M2-alpha-b",
        )
        after_alpha = marker_snapshot(self.marker.history)
        before_beta_initialize = after_alpha["initialize_b"]
        beta = await self.edge.run(
            self.beta_session,
            McpPrompts.positive(b),
            "M2-beta-stable",
        )
        after_beta = marker_snapshot(self.marker.history)
        self.acceptance.completed_invalidate("M2", invalidation)
        self.acceptance.positive("M2-alpha", alpha, b)
        if after_alpha["tool_call_a"] != before_alpha["tool_call_a"]:
            self.acceptance.fail("M2-alpha-called-unbound-a")
        self.acceptance.positive("M2-beta", beta, b)
        if after_beta["initialize_b"] != before_beta_initialize:
            self.acceptance.fail("M2-beta-reinitialized-after-alpha-change")
        self.acceptance.beta_live_stable("M2", beta)
        self.save(
            "M2",
            {
                "projection": projection,
                "invalidation": invalidation,
                "alpha": asdict(alpha),
                "beta": asdict(beta),
                "marker_before_alpha": before_alpha,
                "marker_after_alpha": after_alpha,
                "marker_after_beta": after_beta,
            },
        )

    async def _m3(self) -> None:
        a = self.profiles.identity("a")
        b = self.profiles.identity("b")
        reset_projection = await self.profiles.bind("alpha", "a")
        reset_invalidation = await self.edge.invalidate(
            self.alpha_session, "M3-alpha-reset-invalidate"
        )
        baseline_run = await self.edge.run(
            self.alpha_session,
            McpPrompts.positive(a),
            "M3-alpha-a-baseline",
        )
        self.acceptance.completed_invalidate("M3-reset", reset_invalidation)
        self.acceptance.positive("M3-baseline", baseline_run, a)
        self.marker.reset_hold()
        before_old = marker_snapshot(self.marker.history)
        old_task = self.edge.launch_run(
            self.alpha_session,
            McpPrompts.hold(a),
            "M3-alpha-old",
        )
        try:
            await asyncio.wait_for(self.marker.hold_entered.wait(), timeout=75)
        except asyncio.TimeoutError:
            self.acceptance.fail("M3-hold-not-entered")
            self.marker.release_all_holds()
            old_run = await old_task
            self.save(
                "M3",
                {
                    "reset_projection": reset_projection,
                    "reset_invalidation": reset_invalidation,
                    "baseline_run": asdict(baseline_run),
                    "old_run": asdict(old_run),
                    "blocked": "hold_tool_not_visible",
                },
            )
            return
        updated_projection = await self.profiles.bind("alpha", "b")
        working_invalidation = await self.edge.invalidate(
            self.alpha_session, "M3-alpha-working-invalidate"
        )
        self.marker.release_all_holds()
        old_run = await old_task
        after_old = marker_snapshot(self.marker.history)
        idle_invalidation = await self.edge.invalidate(
            self.alpha_session, "M3-alpha-idle-invalidate"
        )
        new_run = await self.edge.run(
            self.alpha_session,
            McpPrompts.positive(b, negative=a),
            "M3-alpha-new-b",
        )
        before_beta_initialize = marker_count(
            self.marker.history, connection="b", event="initialize"
        )
        beta = await self.edge.run(
            self.beta_session,
            McpPrompts.positive(b),
            "M3-beta-stable",
        )
        after_beta_initialize = marker_count(
            self.marker.history, connection="b", event="initialize"
        )
        keeper = await self.edge.run(
            self.keeper_session,
            McpPrompts.positive(a),
            "M3-a-registry-keeper-stable",
        )
        self.acceptance.working_busy("M3", working_invalidation)
        self.acceptance.positive("M3-old", old_run, a)
        if after_old["tool_call_a"] - before_old["tool_call_a"] < 2:
            self.acceptance.fail("M3-old-run-did-not-complete-a-hold-and-marker")
        self.acceptance.completed_invalidate("M3-idle", idle_invalidation)
        self.acceptance.positive("M3-new", new_run, b)
        self.acceptance.positive("M3-beta", beta, b)
        if after_beta_initialize != before_beta_initialize:
            self.acceptance.fail("M3-beta-reinitialized-after-alpha-change")
        self.acceptance.beta_live_stable("M3", beta)
        self.acceptance.positive("M3-keeper", keeper, a)
        if not self.keeper_live_ids or not set(self.keeper_live_ids).intersection(
            keeper.provider_live_ids
        ):
            self.acceptance.fail("M3-a-registry-keeper-live-agent-changed")
        self.save(
            "M3",
            {
                "reset_projection": reset_projection,
                "reset_invalidation": reset_invalidation,
                "baseline_run": asdict(baseline_run),
                "updated_projection": updated_projection,
                "working_invalidation": working_invalidation,
                "old_run": asdict(old_run),
                "idle_invalidation": idle_invalidation,
                "new_run": asdict(new_run),
                "beta": asdict(beta),
                "keeper": asdict(keeper),
                "marker_before_old": before_old,
                "marker_after_old": after_old,
            },
        )

    def _assert_no_global_reload(self) -> None:
        self.acceptance.no_global_reload()

    def summarize(self) -> dict[str, Any]:
        acceptance = {
            "passed": not self.acceptance.failures,
            "failures": self.acceptance.failures,
        }
        self.store.checkpoint.evidence["acceptance"] = acceptance
        self.store.save()
        return {
            "target": {
                "user_code": self.target.user_code,
                "user_id": self.target.principal.user_id,
                "workspace_id": self.target.workspace_id,
                "runtime_id": self.target.runtime_id,
            },
            "baseline": self.baseline,
            "registry": marker_snapshot(self.marker.history),
            "acceptance": acceptance,
            "checkpoint": str(self.store.path),
        }
