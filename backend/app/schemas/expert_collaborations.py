from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ExpertCollaborationSummary(BaseModel):
    collaboration_id: str
    runtime_workspace_id: int
    runtime_session_id: str
    origin_runtime_run_id: str
    final_runtime_run_id: str | None = None
    state: str
    revision: int
    task_count: int = 0
    last_error_code: str | None = None
    updated_at: datetime


class ExpertCollaborationListResponse(BaseModel):
    collaborations: list[ExpertCollaborationSummary] = Field(default_factory=list)
    pending_count: int = 0
