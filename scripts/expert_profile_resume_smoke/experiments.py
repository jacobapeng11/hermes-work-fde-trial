"""E0-E7 experiment orchestration without transport or database dependencies."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from typing import Any

from .contracts import CheckpointStore, SmokeError, Target
from .marker_server import MarkerMcp
from .profile_fixture import ProfileFixture
from .resume_acceptance import acceptance_failures
from .runtime_edge import RuntimeEdge


class Experiments:
    def __init__(
        self,
        *,
        target: Target,
        baseline: dict[str, Any],
        store: CheckpointStore,
        marker: MarkerMcp,
        profiles: ProfileFixture,
        edge: RuntimeEdge,
    ) -> None:
        self.target = target
        self.baseline = baseline
        self.store = store
        self.marker = marker
        self.profiles = profiles
        self.edge = edge

    @property
    def suffix(self) -> str:
        return self.store.checkpoint.suffix

    def save_experiment(self, name: str, evidence: dict[str, Any]) -> None:
        self.store.checkpoint.evidence[name] = evidence
        self.store.save()

    def alpha_prompt(self, version: str) -> str:
        return (
            f"确定性验收：先调用 skill_view 读取 resume-alpha-{self.suffix}，"
            f"再调用完整名称包含 resume-alpha-{version}-{self.suffix} 且以 "
            f"get_alpha_{version}_marker 结尾的 MCP 工具。原样报告 Skill marker、"
            "MCP marker 和 boot_id。"
        )

    def beta_prompt(self) -> str:
        return (
            f"确定性验收：先调用 skill_view 读取 resume-beta-{self.suffix}，"
            f"再调用完整名称包含 resume-beta-static-{self.suffix} 且以 "
            "get_beta_static_marker 结尾的 MCP 工具。原样报告两个 marker 和 boot_id。"
        )

    def hold_prompt(self, *, after: str = "finish") -> str:
        tail = {
            "finish": "hold 返回后原样报告结果并结束。",
            "skill": (
                f"hold 返回后再次调用 skill_view 读取 resume-alpha-{self.suffix}，"
                "分别报告前后两次 Skill marker。"
            ),
            "mcp": (
                f"hold 返回后尝试调用完整名称包含 resume-alpha-v2-{self.suffix} 且以 "
                "get_alpha_v2_marker 结尾的工具，精确报告成功或不可用。"
            ),
        }[after]
        return (
            f"确定性并发验收：先调用 skill_view 读取 resume-alpha-{self.suffix}；"
            f"然后调用完整名称包含 resume-alpha-control-{self.suffix} 且以 "
            f"hold_marker 结尾的工具。{tail}不要使用 terminal，不要猜测。"
        )

    async def reset_alpha_v1(self, label: str) -> dict[str, Any]:
        projection = await self.profiles.project_alpha(
            skill_version="v1",
            mcp_version="v1",
        )
        self.store.checkpoint.evidence.setdefault("resets", {})[label] = projection
        self.store.save()
        return projection

    async def execute(self) -> dict[str, Any]:
        alpha = await self.profiles.create_expert("alpha")
        beta = await self.profiles.create_expert("beta")
        keeper = await self.profiles.create_expert("registry-keeper")
        self.store.checkpoint.evidence["initial_projection"] = {
            "alpha": await self.profiles.project_alpha(
                skill_version="v1",
                mcp_version="v1",
            ),
            "beta": await self.profiles.project_beta(),
            "registry_keeper": await self.profiles.project_registry_keeper(),
        }
        self.store.save()
        alpha_id = int(alpha["runtime_agent_profile_id"])
        beta_id = int(beta["runtime_agent_profile_id"])
        keeper_id = int(keeper["runtime_agent_profile_id"])

        await self._register_alpha_mcp_set(keeper_id)
        e0_alpha, e0_beta = await self._e0(alpha_id, beta_id)
        await self._e1(alpha_id)
        await self._e2(alpha_id)
        await self._e3(alpha_id)
        await self._e4(alpha_id)
        await self._e5_e6(alpha_id, e0_beta)
        await self._e7(alpha_id, e0_beta)
        self.assert_no_global_reload()
        self.store.save()
        return self.summarize()

    async def _register_alpha_mcp_set(self, keeper_id: int) -> None:
        session = await self.edge.create_session(keeper_id, "registry-keeper")
        run = await self.edge.run(
            session,
            (
                f"确定性全局 MCP 预注册：依次调用完整名称包含 "
                f"resume-alpha-v1-{self.suffix} 且以 get_alpha_v1_marker 结尾的工具，"
                f"以及完整名称包含 resume-alpha-v2-{self.suffix} 且以 "
                "get_alpha_v2_marker 结尾的工具；原样报告两个 marker 和 boot_id。"
            ),
            "registry-keeper",
        )
        evidence = asdict(run)
        rendered = json.dumps(evidence, ensure_ascii=False)
        required = (
            f"ALPHA_V1_{self.suffix}",
            f"ALPHA_V2_{self.suffix}",
        )
        if run.error or any(marker not in rendered for marker in required):
            raise SmokeError("registry keeper did not register both Alpha MCP versions")
        self.save_experiment("registry_keeper", {"run": evidence})

    async def _e0(
        self,
        alpha_id: int,
        beta_id: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        alpha = await self.edge.create_session(alpha_id, "e0-alpha")
        beta = await self.edge.create_session(beta_id, "e0-beta")
        default = await self.edge.create_session(None, "e0-default")
        evidence = {
            "alpha": asdict(
                await self.edge.run(alpha, self.alpha_prompt("v1"), "E0-alpha")
            ),
            "beta": asdict(
                await self.edge.run(beta, self.beta_prompt(), "E0-beta")
            ),
            "alpha_cross": asdict(
                await self.edge.run(
                    alpha,
                    f"只调用 skill_view 尝试读取 resume-beta-{self.suffix}，"
                    "精确报告 not found；禁止其他工具。",
                    "E0-alpha-cross",
                )
            ),
            "beta_cross": asdict(
                await self.edge.run(
                    beta,
                    f"只调用 skill_view 尝试读取 resume-alpha-{self.suffix}，"
                    "精确报告 not found；禁止其他工具。",
                    "E0-beta-cross",
                )
            ),
            "default_negative": asdict(
                await self.edge.run(
                    default,
                    f"只调用 skill_view 尝试读取 resume-alpha-{self.suffix}，"
                    "精确报告 not found；禁止其他工具。",
                    "E0-default",
                )
            ),
        }
        self.save_experiment("E0", evidence)
        return alpha, beta

    async def _e1(self, alpha_id: int) -> None:
        await self.reset_alpha_v1("E1")
        session = await self.edge.create_session(alpha_id, "e1")
        self.marker.reset_hold()
        old_task = self.edge.launch_run(
            session,
            self.hold_prompt(after="finish"),
            "E1-old",
        )
        await asyncio.wait_for(self.marker.hold_entered.wait(), timeout=60)
        await self.profiles.project_alpha(skill_version="v2", mcp_version="v2")
        working_prepare = await self.edge.prepare_probe(session, "E1-working-prepare")
        self.marker.release_all_holds()
        old_run = await old_task
        idle_invalidate = await self.edge.invalidate(session, "E1-idle-invalidate")
        new_run = await self.edge.run(
            session,
            self.alpha_prompt("v2"),
            "E1-after-idle-resume",
        )
        self.save_experiment(
            "E1",
            {
                "working_prepare": working_prepare,
                "old_run": asdict(old_run),
                "idle_invalidate": idle_invalidate,
                "new_run": asdict(new_run),
            },
        )

    async def _e2(self, alpha_id: int) -> None:
        await self.reset_alpha_v1("E2")
        session = await self.edge.create_session(alpha_id, "e2")
        self.marker.reset_hold()
        old_task = self.edge.launch_run(
            session,
            self.hold_prompt(after="finish"),
            "E2-old",
        )
        await asyncio.wait_for(self.marker.hold_entered.wait(), timeout=60)
        await self.profiles.project_alpha(skill_version="v2", mcp_version="v2")
        working_invalidate = await self.edge.invalidate(
            session,
            "E2-working-invalidate",
        )
        self.marker.release_all_holds()
        old_run = await old_task
        self.save_experiment(
            "E2",
            {
                "working_invalidate": working_invalidate,
                "old_run": asdict(old_run),
                "raw_dashboard_close": {
                    "status": "not_exposed",
                    "reason": (
                        "current external Runtime control contract has no raw working "
                        "session.close action"
                    ),
                },
            },
        )

    async def _e3(self, alpha_id: int) -> None:
        await self.reset_alpha_v1("E3")
        session = await self.edge.create_session(alpha_id, "e3")
        self.marker.reset_hold()
        same_task = self.edge.launch_run(
            session,
            self.hold_prompt(after="skill"),
            "E3-same-run",
        )
        await asyncio.wait_for(self.marker.hold_entered.wait(), timeout=60)
        await self.profiles.project_alpha(skill_version="v2", mcp_version="v1")
        self.marker.release_all_holds()
        same_run = await same_task
        idle_invalidate = await self.edge.invalidate(session, "E3-idle-invalidate")
        new_run = await self.edge.run(
            session,
            f"只调用 skill_view 读取 resume-alpha-{self.suffix} 并原样报告 Skill marker。",
            "E3-after-resume",
        )
        self.save_experiment(
            "E3",
            {
                "same_run": asdict(same_run),
                "idle_invalidate": idle_invalidate,
                "new_run": asdict(new_run),
            },
        )

    async def _e4(self, alpha_id: int) -> None:
        await self.reset_alpha_v1("E4")
        session = await self.edge.create_session(alpha_id, "e4")
        self.marker.reset_hold()
        same_task = self.edge.launch_run(
            session,
            self.hold_prompt(after="mcp"),
            "E4-same-run",
        )
        await asyncio.wait_for(self.marker.hold_entered.wait(), timeout=60)
        await self.profiles.project_alpha(skill_version="v1", mcp_version="v2")
        self.marker.release_all_holds()
        same_run = await same_task
        idle_invalidate = await self.edge.invalidate(session, "E4-idle-invalidate")
        new_run = await self.edge.run(
            session,
            self.alpha_prompt("v2"),
            "E4-after-resume",
        )
        self.save_experiment(
            "E4",
            {
                "same_run": asdict(same_run),
                "idle_invalidate": idle_invalidate,
                "new_run": asdict(new_run),
            },
        )

    async def _e5_e6(
        self,
        alpha_id: int,
        beta_session: dict[str, Any],
    ) -> None:
        await self.reset_alpha_v1("E5")
        session = await self.edge.create_session(alpha_id, "e5")
        self.marker.reset_hold()
        old_task = self.edge.launch_run(
            session,
            self.hold_prompt(after="finish"),
            "E5-old",
        )
        await asyncio.wait_for(self.marker.hold_entered.wait(), timeout=60)
        await self.profiles.project_alpha(skill_version="v2", mcp_version="v2")
        working_invalidate = await self.edge.invalidate(
            session,
            "E5-working-invalidate",
        )
        queued = asyncio.Event()
        next_task = self.edge.launch_run(
            session,
            self.alpha_prompt("v2"),
            "E5-next",
            accepted=queued,
        )
        beta_during = await self.edge.run(
            beta_session,
            self.beta_prompt(),
            "E6-beta-during",
        )
        try:
            await asyncio.wait_for(queued.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
        self.marker.release_all_holds()
        old_run, next_run = await asyncio.gather(old_task, next_task)
        self.save_experiment(
            "E5",
            {
                "working_invalidate": working_invalidate,
                "old_run": asdict(old_run),
                "next_run": asdict(next_run),
            },
        )
        self.save_experiment("E6", {"beta_during": asdict(beta_during)})

    async def _e7(
        self,
        alpha_id: int,
        beta_session: dict[str, Any],
    ) -> None:
        await self.reset_alpha_v1("E7")
        session = await self.edge.create_session(alpha_id, "e7")
        v1 = await self.edge.run(session, self.alpha_prompt("v1"), "E7-v1")
        await self.profiles.project_alpha(skill_version="v2", mcp_version="v2")
        idle_invalidate = await self.edge.invalidate(session, "E7-idle-invalidate")
        v2 = await self.edge.run(session, self.alpha_prompt("v2"), "E7-v2")
        beta_after = await self.edge.run(
            beta_session,
            self.beta_prompt(),
            "E6-beta-after",
        )
        self.save_experiment(
            "E7",
            {
                "v1": asdict(v1),
                "idle_invalidate": idle_invalidate,
                "v2": asdict(v2),
            },
        )
        self.store.checkpoint.evidence["E6"]["beta_after"] = asdict(beta_after)
        self.store.save()

    def assert_no_global_reload(self) -> None:
        forbidden = {"skills.reload", "reload.mcp"}
        observed = {
            str(item.get("method"))
            for item in self.store.checkpoint.control_methods
        }
        overlap = sorted(observed & forbidden)
        if overlap:
            raise SmokeError(f"global reload method observed: {overlap}")
        self.store.checkpoint.evidence["no_global_reload"] = {
            "status": "pass",
            "audited_methods": sorted(observed),
            "scope": "smoke script and exercised platform control path",
        }

    def summarize(self) -> dict[str, Any]:
        rendered = json.dumps(self.store.checkpoint.evidence, ensure_ascii=False)
        markers = {
            "alpha_v1": f"ALPHA_V1_{self.suffix}" in rendered,
            "alpha_v2": f"ALPHA_V2_{self.suffix}" in rendered,
            "beta_static": f"BETA_STATIC_{self.suffix}" in rendered,
            "skill_v1": f"ALPHA_SKILL_V1_{self.suffix}" in rendered,
            "skill_v2": f"ALPHA_SKILL_V2_{self.suffix}" in rendered,
        }
        failures = acceptance_failures(
            suffix=self.suffix,
            evidence=self.store.checkpoint.evidence,
            markers=markers,
        )
        acceptance = {"passed": not failures, "failures": failures}
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
            "markers_observed": markers,
            "acceptance": acceptance,
            "checkpoint": str(self.store.path),
        }
