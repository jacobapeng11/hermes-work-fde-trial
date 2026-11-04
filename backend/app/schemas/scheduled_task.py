from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ScheduledTaskCommand(BaseModel):
    """Wire command shared by create and update endpoints.

    The numeric database Session id is intentionally not part of this contract;
    callers address a Session by its stable runtime-session string.
    """

    model_config = ConfigDict(extra="forbid")

    task_name: str = Field(min_length=1, max_length=200)
    prompt: str = Field(max_length=20_000)
    target_runtime_session_id: str = Field(min_length=1, max_length=128)
    schedule_config: dict[str, Any]

    @field_validator("task_name", mode="before")
    @classmethod
    def normalize_task_name(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip()

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must not be blank")
        return value


class CreateScheduledTaskRequest(ScheduledTaskCommand):
    pass


class UpdateScheduledTaskRequest(ScheduledTaskCommand):
    pass


class ScheduledTaskSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: int
    task_name: str
    prompt: str
    status: str
    schedule_config: dict[str, Any]
    schedule_summary: str
    target_runtime_session_id: str
    target_session_title: str | None = None
    target_session_status: str | None = None
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_run_status: str | None = None
    last_error_code: str | None = None
    last_error: str | None = None
    last_runtime_run_id: str | None = None
    run_count: int = 0
    is_running: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ScheduledTaskListResponse(BaseModel):
    tasks: list[ScheduledTaskSummary]


class ScheduledTaskTriggerResponse(BaseModel):
    task_id: int
    target_runtime_session_id: str
    accepted_at: datetime


# Compatibility aliases for callers that use the shorter names from the design.
ScheduledTaskRequest = ScheduledTaskCommand
ScheduledTaskResponse = ScheduledTaskSummary
