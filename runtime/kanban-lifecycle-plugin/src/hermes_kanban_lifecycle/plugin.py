from __future__ import annotations

import json
import re
import time
from typing import Any

from .platform_client import PlatformClient, PlatformClientError


_KANBAN_TOOLS = {
    "kanban_create",
    "kanban_claim",
    "kanban_complete",
    "kanban_block",
    "kanban_unblock",
    "kanban_heartbeat",
    "kanban_link",
    "kanban_comment",
}
_LIFECYCLE_EVENTS = (
    "kanban_task_claimed",
    "kanban_task_completed",
    "kanban_task_blocked",
)
_EXPERT_ASSIGNEE = re.compile(r"expert-[1-9][0-9]*")


def _tool_args(kwargs: dict[str, Any]) -> dict[str, Any]:
    value = kwargs.get("args") or kwargs.get("tool_input")
    return value if isinstance(value, dict) else {}


def _expert_task_has_skills(kwargs: dict[str, Any]) -> bool:
    args = _tool_args(kwargs)
    assignee = str(args.get("assignee") or "").strip()
    if _EXPERT_ASSIGNEE.fullmatch(assignee) is None:
        return False
    skills = args.get("skills")
    if skills is None:
        return False
    if isinstance(skills, str):
        return bool(skills.strip())
    if isinstance(skills, (list, tuple, set, dict)):
        return bool(skills)
    return True


def _tool_event_id(event_name: str, kwargs: dict[str, Any]) -> str:
    tool_call_id = str(kwargs.get("tool_call_id") or "unknown")
    return f"{event_name}:{tool_call_id}"[:240]


def _lifecycle_event_id(event_name: str, kwargs: dict[str, Any]) -> str:
    return ":".join(
        (
            event_name,
            str(kwargs.get("task_id") or "unknown"),
            str(kwargs.get("run_id") or ""),
            str(kwargs.get("profile_name") or "default"),
        )
    )[:240]


def _created_task_id(kwargs: dict[str, Any]) -> str | None:
    result = kwargs.get("result")
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    value = str(parsed.get("task_id") or "").strip()
    return value[:100] if value else None


def register(ctx) -> None:
    client = PlatformClient()

    def pre_tool_call(**kwargs: Any):
        if kwargs.get("tool_name") != "kanban_create":
            return None
        if _expert_task_has_skills(kwargs):
            return {
                "action": "block",
                "message": (
                    "Installed expert Profiles already load their configured "
                    "capabilities. Retry kanban_create with the same expert-* "
                    "assignee and omit the skills parameter entirely. Do not copy "
                    "Skill or MCP names from expert_list into task-level skills."
                ),
            }
        try:
            client.emit(
                {
                    "event_id": _tool_event_id("pre_tool_call", kwargs),
                    "event_name": "pre_tool_call",
                    "tool_name": "kanban_create",
                    "task_id": kwargs.get("task_id"),
                }
            )
        except PlatformClientError:
            # A card must not be created in the gap before the control plane
            # can hold its temporary keepalive lease.
            return {
                "action": "block",
                "message": "Kanban 运行环境保活暂不可用，请稍后重试创建任务。",
            }
        return None

    def post_tool_call(**kwargs: Any):
        tool_name = str(kwargs.get("tool_name") or "")
        if tool_name not in _KANBAN_TOOLS:
            return None
        args = _tool_args(kwargs)
        task_id = kwargs.get("task_id")
        if tool_name == "kanban_create":
            task_id = _created_task_id(kwargs) or task_id
        try:
            client.emit(
                {
                    "event_id": _tool_event_id("post_tool_call", kwargs),
                    "event_name": "post_tool_call",
                    "tool_name": tool_name,
                    "task_id": task_id,
                    "board": args.get("board"),
                    "runtime_session_id": kwargs.get("session_id"),
                    "tool_call_id": kwargs.get("tool_call_id"),
                    "turn_id": kwargs.get("turn_id"),
                    "assignee": args.get("assignee"),
                    "outcome": str(kwargs.get("status") or "unknown")[:24],
                }
            )
        except PlatformClientError:
            # The pre-create lease is durable and Backend reconciliation is
            # authoritative, so post/lifecycle notifications remain fail-open.
            return None
        return None

    def lifecycle(event_name: str):
        def observer(**kwargs: Any):
            attempts = 3 if event_name == "kanban_task_claimed" else 1
            responses_seen: list[dict[str, Any]] = []
            for attempt in range(attempts):
                try:
                    response = client.emit(
                        {
                            "event_id": _lifecycle_event_id(
                                event_name, kwargs
                            ),
                            "event_name": event_name,
                            "task_id": kwargs.get("task_id"),
                            "board": kwargs.get("board"),
                            "assignee": kwargs.get("assignee"),
                            "profile_name": kwargs.get("profile_name"),
                            "outcome": "ok",
                        }
                    )
                    responses_seen.append(response)
                    if (
                        event_name != "kanban_task_claimed"
                        or response.get("worker_route_ready") is not False
                    ):
                        break
                except PlatformClientError:
                    pass
                if attempt + 1 < attempts:
                    time.sleep(0.2 * (attempt + 1))
            if event_name == "kanban_task_claimed" and not any(
                # The last response is the only authoritative readiness
                # result.  Never leave a claimed worker running without a
                # route lease; converge it to a terminal blocked event.
                response.get("worker_route_ready") is not False
                for response in responses_seen
            ):
                try:
                    client.emit(
                        {
                            "event_id": _lifecycle_event_id(
                                "kanban_task_blocked", kwargs
                            ),
                            "event_name": "kanban_task_blocked",
                            "task_id": kwargs.get("task_id"),
                            "board": kwargs.get("board"),
                            "assignee": kwargs.get("assignee"),
                            "profile_name": kwargs.get("profile_name"),
                            "outcome": "route_activation_failed",
                        }
                    )
                except PlatformClientError:
                    pass
            return None

        return observer

    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    for event_name in _LIFECYCLE_EVENTS:
        ctx.register_hook(event_name, lifecycle(event_name))
