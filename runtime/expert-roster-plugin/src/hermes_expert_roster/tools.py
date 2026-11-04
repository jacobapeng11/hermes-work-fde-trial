from __future__ import annotations

import json
from typing import Any

from .platform_client import PlatformClient, PlatformClientError


def handlers(client: PlatformClient | None = None) -> dict[str, Any]:
    client = client or PlatformClient()

    def expert_list(_params: dict[str, Any], **_context: Any) -> str:
        try:
            return json.dumps(client.list_experts(), ensure_ascii=False)
        except (PlatformClientError, TypeError, ValueError) as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    return {"expert_list": expert_list}


TOOL_SCHEMAS = {
    "expert_list": {
        "description": (
            "List the current user's installed and ready expert Profiles, including "
            "their exact Hermes assignee names, descriptions, and configured "
            "capability names. You MUST call this whenever the user names an "
            "installed expert, explicitly requests expert collaboration, or before "
            "creating expert work when the correct assignee is unclear. Delegate "
            "expert work only with native kanban_create using the exact assignee "
            "returned by this tool. Never use delegate_task as a substitute for an "
            "installed expert; delegate_task remains available only for ordinary "
            "ephemeral subtasks unrelated to expert Profiles. If roster lookup or "
            "kanban_create fails, report that expert dispatch failed instead of "
            "falling back to a temporary subagent. For a complex multi-expert "
            "request, prefer one triage root task assigned to the default "
            "orchestrator so Hermes Auto Decompose can build the dependency graph. "
            "The capabilities returned here are descriptive routing metadata only. "
            "Installed expert Profiles already load their configured Skills, MCPs, "
            "knowledge bases, and Plugins. When kanban_create targets an expert-* "
            "assignee, omit the skills parameter entirely: never copy or invent "
            "capability names as task-level skills, and never treat an MCP name as "
            "a Skill."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }
}
