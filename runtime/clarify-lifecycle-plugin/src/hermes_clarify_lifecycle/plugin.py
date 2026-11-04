from __future__ import annotations

import time
import os
from typing import Any

from .platform_client import (
    PlatformClient,
    PlatformClientError,
    current_runtime_agent_profile_id,
)


_PARK_PREFIX = "__HERMES_CLARIFY_PARK__:"
_DISABLED_MESSAGE = "Clarify tool is disabled in this execution context. Continue with a safe reasonable default."
_WORKER_TIMEOUT_MESSAGE = "用户未在时限内回答确认问题。Continue with a safe reasonable default."
_WORKER_POLL_SECONDS = 1.0


def _worker_wait_seconds() -> float:
    raw = os.environ.get("HERMES_CLARIFY_WORKER_WAIT_SECONDS", "")
    try:
        value = float(raw) if raw else 75.0
    except ValueError:
        value = 75.0
    return min(max(value, 0.0), 600.0)


def _is_application_agent_context() -> bool:
    # The Knative provider sets this to "0" only for Application Agent
    # Runtimes. Personal default/expert Profiles always run with the Kanban
    # orchestrator enabled, and their clarify requests stay eligible.
    return os.environ.get("HERMES_KANBAN_ORCHESTRATOR_ENABLED", "") == "0"


def _answer_text(answer: object) -> str:
    if not isinstance(answer, dict):
        return ""
    kind = str(answer.get("kind") or "")
    if kind == "multi":
        return "、".join(
            str(item) for item in answer.get("values", []) if str(item)
        )
    if kind == "skip":
        return "用户跳过了该问题，请自行采用合理默认值。"
    return str(answer.get("text") or "")


def _text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _choices(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:4]:
        if isinstance(item, str):
            text = _text(item, limit=512)
            if text:
                result.append(text)
    return result


def register(ctx) -> None:
    client = PlatformClient()

    def _wait_worker_answer(receipt: dict[str, Any]) -> dict[str, Any]:
        # The worker keeps running inside this synchronous hook; Backend owns
        # the 60s auto-default, so the local deadline only bounds tail latency.
        deadline = time.monotonic() + _worker_wait_seconds()
        while True:
            try:
                status = client.get_request(str(receipt["request_id"]))
            except PlatformClientError:
                status = None
            if isinstance(status, dict):
                state = str(status.get("status") or "")
                if state == "answered":
                    rendered = _answer_text(status.get("answer"))
                    if rendered:
                        return {
                            "action": "block",
                            "message": (
                                "用户已通过确认卡片回答："
                                f"{rendered}。请基于该答案继续执行当前任务，"
                                "不要重复提问。"
                            ),
                        }
                if state in {"canceled", "timed_out"}:
                    return {"action": "block", "message": _WORKER_TIMEOUT_MESSAGE}
            if time.monotonic() >= deadline:
                return {"action": "block", "message": _WORKER_TIMEOUT_MESSAGE}
            time.sleep(_WORKER_POLL_SECONDS)

    def pre_tool_call(**kwargs: Any):
        if kwargs.get("tool_name") != "clarify":
            return None
        if _is_application_agent_context():
            # Application Agent traffic (OpenAPI/WeCom/Studio) has no
            # Clarify card surface. Backend would reject the request with a
            # personal-Binding auth failure anyway; skip the doomed call and
            # keep the model moving with a safe default.
            return {"action": "block", "message": _DISABLED_MESSAGE}
        args = kwargs.get("args") or kwargs.get("tool_input")
        args = args if isinstance(args, dict) else {}
        question = _text(args.get("question"), limit=4096)
        if not question:
            return {"action": "block", "message": _DISABLED_MESSAGE}
        payload = {
            "event_id": f"clarify:{kwargs.get('tool_call_id') or 'unknown'}"[:240],
            "runtime_session_id": _text(kwargs.get("session_id"), limit=200),
            "tool_call_id": _text(kwargs.get("tool_call_id"), limit=200),
            "turn_id": _text(kwargs.get("turn_id"), limit=200),
            "question": question,
            "choices": _choices(args.get("choices")),
            "multi_select": bool(args.get("multi_select")),
        }
        profile_id = current_runtime_agent_profile_id()
        if profile_id is not None:
            payload["runtime_agent_profile_id"] = profile_id
        try:
            receipt = {"enabled": False}
            # Dashboard events are FIFO, but the hook can arrive a few
            # milliseconds before Backend has projected run.accepted.  Retry
            # only that safe, bounded race; never fall through to Hermes'
            # in-process blocking clarify callback.
            for delay in (0.0, 0.15, 0.35, 0.7):
                if delay:
                    time.sleep(delay)
                receipt = client.create_request(payload)
                if bool(receipt.get("enabled")):
                    break
        except PlatformClientError:
            # Fail closed: falling through would invoke Hermes' in-process
            # blocking callback and recreate the session-busy failure mode.
            return {"action": "block", "message": _DISABLED_MESSAGE}
        if not bool(receipt.get("enabled")):
            return {"action": "block", "message": _DISABLED_MESSAGE}
        if receipt.get("mode") == "expert_worker":
            return _wait_worker_answer(receipt)
        return {
            "action": "block",
            "message": _PARK_PREFIX + str(receipt["request_id"]),
        }

    ctx.register_hook("pre_tool_call", pre_tool_call)
