"""One focused active-tool Supervisor MCP reload experiment."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import time
from typing import Any

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.hermes_runtime.dashboard_protocol import HermesDashboardProtocolAdapter
from app.models import RuntimeRun

from expert_profile_resume_smoke.contracts import (
    CheckpointStore,
    RunEvidence,
    SmokeError,
    Target,
)
from expert_profile_resume_smoke.marker_server import MarkerMcp
from expert_profile_resume_smoke.mcp_contracts import marker_count, marker_snapshot
from expert_profile_resume_smoke.mcp_prompts import McpPrompts
from expert_profile_resume_smoke.runtime_edge import RuntimeEdge

from .business_mcp_fixture import BusinessMcpProfileFixture
from .raw_dashboard import RuntimePod


class ActiveReloadExperiment:
    def __init__(
        self,
        *,
        target: Target,
        baseline: dict[str, Any],
        store: CheckpointStore,
        marker: MarkerMcp,
        profiles: BusinessMcpProfileFixture,
        edge: RuntimeEdge,
        control: HermesDashboardProtocolAdapter,
        pod: RuntimePod,
    ) -> None:
        self.target = target
        self.baseline = baseline
        self.store = store
        self.marker = marker
        self.profiles = profiles
        self.edge = edge
        self.control = control
        self.pod = pod

    async def execute(self) -> dict[str, Any]:
        expert = await self.profiles.create_expert("active-mcp-reload")
        self.profiles.experts["alpha"] = expert
        projection = await self.profiles.bind("alpha", "a")
        session = await self.edge.create_session(
            int(expert["runtime_agent_profile_id"]), "active-mcp-reload"
        )
        identity = self.profiles.identity("a")
        baseline_run = await self.edge.run(
            session,
            McpPrompts.positive(identity),
            "R0-registry-ready",
        )
        if baseline_run.error or identity.marker not in _rendered(baseline_run):
            raise SmokeError("baseline MCP marker Run did not establish Registry readiness")

        self.marker.reset_hold()
        before = marker_snapshot(self.marker.history)
        hold_task = self.edge.launch_run(
            session,
            McpPrompts.hold(identity),
            "R1-active-supervisor-reload",
        )
        reload_task: asyncio.Task[dict[str, Any]] | None = None
        try:
            await asyncio.wait_for(self.marker.hold_entered.wait(), timeout=75)
            active_runs = await self._require_only_test_run(hold_task)
            self.store.record_control(
                "supervisor.reload_mcp",
                pod=self.pod.name,
                run_id=self.store.checkpoint.run_ids[-1],
            )
            reload_task = asyncio.create_task(self._reload_once())
            done, _ = await asyncio.wait({reload_task}, timeout=2)
            pending_while_hold = not done
        finally:
            self.marker.release_all_holds()

        if reload_task is None:
            raise SmokeError("Supervisor reload was not dispatched")
        reload_result = await asyncio.wait_for(reload_task, timeout=130)
        active_run = await asyncio.wait_for(hold_task, timeout=150)
        after_active = marker_snapshot(self.marker.history)
        same_session_recovery = await self.edge.run(
            session,
            McpPrompts.positive(identity),
            "R2-same-session-recovery",
        )
        after_same_session = marker_snapshot(self.marker.history)
        fresh_session = await self.edge.create_session(
            int(expert["runtime_agent_profile_id"]), "active-mcp-reload-fresh"
        )
        fresh_session_recovery = await self.edge.run(
            fresh_session,
            McpPrompts.positive(identity),
            "R3-fresh-session-recovery",
        )
        after_fresh_session = marker_snapshot(self.marker.history)
        evidence = {
            "projection": projection,
            "pod": {
                "namespace": self.pod.namespace,
                "name": self.pod.name,
                "image": self.pod.image,
                "image_id": self.pod.image_id,
            },
            "baseline_run": asdict(baseline_run),
            "active_gate": {"database_runs": active_runs},
            "supervisor_reload": {
                **reload_result,
                "pending_while_hold": pending_while_hold,
            },
            "active_run": asdict(active_run),
            "same_session_recovery": asdict(same_session_recovery),
            "fresh_session_recovery": asdict(fresh_session_recovery),
            "marker_snapshots": {
                "before": before,
                "after_active": after_active,
                "after_same_session": after_same_session,
                "after_fresh_session": after_fresh_session,
            },
            "active_marker_delta": {
                key: after_active[key] - before[key]
                for key in sorted(before)
            },
        }
        evidence["outcome"] = classify_outcome(
            reload_result=reload_result,
            pending_while_hold=pending_while_hold,
            active_run=active_run,
            same_session_recovery=same_session_recovery,
            fresh_session_recovery=fresh_session_recovery,
            marker=identity.marker,
            hold_tool_calls=marker_count(
                self.marker.history,
                connection="a",
                event="tool_call",
                tool=identity.hold_tool,
            ),
            active_marker_tool_delta=(
                after_active["tool_call_a"] - before["tool_call_a"]
            ),
        )
        self.store.checkpoint.evidence["active_supervisor_reload_experiment"] = evidence
        self.store.save()
        return {
            "target": {
                "name": self.target.user_name,
                "user_code": self.target.user_code,
                "user_id": self.target.principal.user_id,
                "workspace_id": self.target.workspace_id,
                "runtime_id": self.target.runtime_id,
            },
            "baseline": self.baseline,
            "outcome": evidence["outcome"],
            "supervisor_reload": evidence["supervisor_reload"],
            "active_run": asdict(active_run),
            "same_session_recovery": asdict(same_session_recovery),
            "fresh_session_recovery": asdict(fresh_session_recovery),
            "checkpoint": str(self.store.path),
        }

    async def _reload_once(self) -> dict[str, Any]:
        started = time.monotonic()
        try:
            await self.control.reload_mcp(self.target.endpoint)
        except Exception as exc:
            return {
                "ok": False,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "error_type": type(exc).__name__,
                "message": str(exc)[:300],
            }
        return {
            "ok": True,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "status": "reloaded",
        }

    async def _require_only_test_run(
        self, task: asyncio.Task[RunEvidence]
    ) -> list[dict[str, str]]:
        if task.done():
            raise SmokeError("hold Run ended before Supervisor reload")
        observed: list[dict[str, str]] = []
        for attempt in range(41):
            if task.done():
                raise SmokeError("hold Run ended before Supervisor reload")
            async with AsyncSessionLocal() as db:
                rows = list(
                    (
                        await db.execute(
                            select(RuntimeRun.runtime_run_id, RuntimeRun.status).where(
                                RuntimeRun.runtime_workspace_id
                                == self.target.workspace_id,
                                RuntimeRun.status.in_(
                                    ("accepted", "pending", "running")
                                ),
                            )
                        )
                    ).all()
                )
            observed = [
                {"runtime_run_id": str(run_id), "status": str(status)}
                for run_id, status in rows
            ]
            observed_ids = {item["runtime_run_id"] for item in observed}
            owned_ids = set(self.store.checkpoint.run_ids)
            if observed_ids <= owned_ids:
                return observed
            if attempt < 40:
                await asyncio.sleep(0.5)
        raise SmokeError(
            "active reload requires exactly the current test Run to be active"
        )


def classify_outcome(
    *,
    reload_result: dict[str, Any],
    pending_while_hold: bool,
    active_run: RunEvidence,
    same_session_recovery: RunEvidence,
    fresh_session_recovery: RunEvidence,
    marker: str,
    hold_tool_calls: int,
    active_marker_tool_delta: int,
) -> dict[str, Any]:
    acknowledged = reload_result.get("ok") is True
    completed = _completed(active_run)
    same_recovered = _mcp_recovered(same_session_recovery, marker)
    fresh_recovered = _mcp_recovered(fresh_session_recovery, marker)
    post_hold_marker_called = active_marker_tool_delta >= 2
    if not acknowledged:
        code = "reload_failed"
    elif hold_tool_calls != 1:
        code = "active_tool_repeated"
    elif not completed:
        code = "run_interrupted_or_failed"
    elif not post_hold_marker_called:
        code = "run_completed_with_mcp_degradation"
    elif not same_recovered and fresh_recovered:
        code = "same_session_agent_stale_after_reload"
    elif not same_recovered or not fresh_recovered:
        code = "post_reload_recovery_failed"
    elif pending_while_hold:
        code = "reload_waited_for_active_tool_then_all_runs_recovered"
    else:
        code = "reload_completed_during_active_tool_and_all_runs_recovered"
    return {
        "code": code,
        "reload_acknowledged": acknowledged,
        "reload_pending_while_hold": pending_while_hold,
        "run_completed": completed,
        "same_session_recovered": same_recovered,
        "fresh_session_recovered": fresh_recovered,
        "hold_tool_calls_total": hold_tool_calls,
        "post_hold_marker_called": post_hold_marker_called,
    }


def _completed(run: RunEvidence) -> bool:
    return (
        run.error is None
        and any(item.get("event") == "chat.done" for item in run.events)
    )


def _mcp_recovered(run: RunEvidence, marker: str) -> bool:
    return _completed(run) and marker in _rendered(run)


def _rendered(run: RunEvidence) -> str:
    return "\n".join((run.reasoning, run.final_text, str(run.tools)))
