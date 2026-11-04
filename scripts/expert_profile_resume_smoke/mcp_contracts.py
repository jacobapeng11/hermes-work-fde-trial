"""Small contracts and evidence helpers for the dual-MCP isolation smoke."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Any

from .contracts import RunEvidence


@dataclass(frozen=True)
class McpIdentity:
    connection: str
    server_name: str
    marker_tool: str
    marker: str
    hold_tool: str | None = None


def rendered_run(run: RunEvidence | dict[str, Any]) -> str:
    payload = asdict(run) if isinstance(run, RunEvidence) else run
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def has_tool_search(run: RunEvidence) -> bool:
    return any("tool_search" in str(item.get("name") or "") for item in run.tools)


def search_results(run: RunEvidence) -> str:
    return json.dumps(
        [
            item.get("result")
            for item in run.tools
            if "tool_search" in str(item.get("name") or "")
        ],
        ensure_ascii=False,
        sort_keys=True,
    )


def marker_count(
    history: list[dict[str, Any]],
    *,
    connection: str,
    event: str,
    tool: str | None = None,
) -> int:
    return sum(
        1
        for item in history
        if item.get("group") == "registry"
        and item.get("version") == connection
        and item.get("event") == event
        and (tool is None or item.get("tool") == tool)
    )


def marker_snapshot(history: list[dict[str, Any]]) -> dict[str, int]:
    return {
        f"{event}_{connection}": marker_count(
            history,
            connection=connection,
            event=event,
        )
        for connection in ("a", "b")
        for event in ("initialize", "tool_call", "hold_entered")
    }
