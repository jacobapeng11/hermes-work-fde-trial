from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RuntimeKanbanLeaseEventRequest(BaseModel):
    event_id: str = Field(min_length=1, max_length=240)
    event_name: Literal[
        "pre_tool_call",
        "post_tool_call",
        "kanban_task_claimed",
        "kanban_task_completed",
        "kanban_task_blocked",
    ]
    runtime_agent_profile_id: int | None = Field(default=None, ge=1)
    tool_name: str | None = Field(default=None, max_length=80)
    task_id: str | None = Field(default=None, max_length=100)
    board: str | None = Field(default=None, max_length=100)
    outcome: str | None = Field(default=None, max_length=24)
    runtime_session_id: str | None = Field(default=None, max_length=200)
    tool_call_id: str | None = Field(default=None, max_length=200)
    turn_id: str | None = Field(default=None, max_length=200)
    assignee: str | None = Field(default=None, max_length=128)
    profile_name: str | None = Field(default=None, max_length=128)


class RuntimeKanbanLeaseEventResponse(BaseModel):
    accepted: bool = True
    duplicate: bool = False
    lease_state: str
    worker_route_ready: bool | None = None
