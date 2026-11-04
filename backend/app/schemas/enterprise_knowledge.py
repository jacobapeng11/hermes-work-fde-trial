from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, StrictBool


class EnterpriseKnowledgeSettingsUpdate(BaseModel):
    enabled: StrictBool


class EnterpriseKnowledgeSettingsResponse(BaseModel):
    runtime_workspace_id: int
    enabled: bool
    mode: str = "full"
    updated_at: datetime | None = None


class RuntimeKnowledgeStateResponse(BaseModel):
    enabled: bool
    mode: str = "full"


class RuntimeKnowledgeSqlContextRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    domain: str | None = Field(default=None, max_length=64)


class RuntimeKnowledgeSqlQueryRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=50_000)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=500)


class RuntimeKnowledgeSqlQueryBatchItem(BaseModel):
    query_id: str | None = None
    sql: str = Field(min_length=1, max_length=50_000)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=500, ge=1, le=500)


class RuntimeKnowledgeSqlQueryBatchRequest(BaseModel):
    queries: list[RuntimeKnowledgeSqlQueryBatchItem] = Field(
        min_length=1, max_length=100
    )


class RuntimeKnowledgeDocumentSearchRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    tag: str | None = Field(default=None, max_length=255)
