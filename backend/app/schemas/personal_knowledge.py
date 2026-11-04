from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


PersonalKnowledgeRole = Literal["owner", "download", "preview", "retrieve"]
PersonalKnowledgeFileStatus = Literal["pending", "parsing", "ready", "failed"]


class PersonalKnowledgeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        normalized = value.strip() if value is not None else None
        return normalized or None


class PersonalKnowledgeUpdate(PersonalKnowledgeCreate):
    pass


class PersonalKnowledgeSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge_base_id: int
    name: str
    description: str | None = None
    owner_user_id: int
    owner_name: str
    effective_role: PersonalKnowledgeRole
    runtime_enabled: bool = False
    file_count: int = 0
    ready_file_count: int = 0
    visibility: Literal["private", "shared"]
    status: str
    created_at: datetime
    updated_at: datetime


class PersonalKnowledgeListResponse(BaseModel):
    items: list[PersonalKnowledgeSummary]
    total: int


class PersonalKnowledgeDetail(PersonalKnowledgeSummary):
    can_enter: bool
    can_preview: bool
    can_download: bool
    can_manage: bool


class PersonalKnowledgePermissionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_ids: list[int] = Field(default_factory=list)
    department_ids: list[str] = Field(default_factory=list)
    all: bool = False


class PersonalKnowledgePermissionReplace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    download: PersonalKnowledgePermissionTarget
    preview: PersonalKnowledgePermissionTarget
    retrieve: PersonalKnowledgePermissionTarget


class PersonalKnowledgeSelectedUser(BaseModel):
    id: int
    name: str | None = None
    user_code: str | None = None
    status: Literal["active", "inactive", "missing"]


class PersonalKnowledgePermissionDepartmentGrant(BaseModel):
    target_type: Literal["department"] = "department"
    target_id: str
    name: str
    department_path: list[str] = Field(default_factory=list)
    role: Literal["download", "preview", "retrieve"]


class PersonalKnowledgePermissionUserGrant(BaseModel):
    target_type: Literal["user"] = "user"
    target_id: int
    name: str | None = None
    user_code: str | None = None
    department_id: str | None = None
    department_path: list[str] = Field(default_factory=list)
    status: Literal["active", "inactive", "missing"]
    role: Literal["download", "preview", "retrieve"]


PersonalKnowledgePermissionGrant = (
    PersonalKnowledgePermissionDepartmentGrant | PersonalKnowledgePermissionUserGrant
)


class PersonalKnowledgePermissionResponse(PersonalKnowledgePermissionReplace):
    normalized_targets: list[str] = Field(default_factory=list)
    selected_users: list[PersonalKnowledgeSelectedUser] = Field(default_factory=list)
    grants: list[PersonalKnowledgePermissionGrant] = Field(default_factory=list)


class PersonalKnowledgeFileSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: int
    filename: str
    size_bytes: int
    mime_type: str | None = None
    status: PersonalKnowledgeFileStatus
    uploader_id: int
    uploader_name: str | None = None
    updated_at: datetime


class PersonalKnowledgeFileListResponse(BaseModel):
    items: list[PersonalKnowledgeFileSummary]
    total: int


class PersonalKnowledgeFileRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=500)

    @field_validator("filename")
    @classmethod
    def normalize_filename(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("filename must not be blank")
        return normalized


class PersonalKnowledgeBindingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    runtime_agent_profile_id: int | None = None


class PersonalKnowledgeBindingResponse(BaseModel):
    runtime_workspace_id: int
    runtime_agent_profile_id: int | None = None
    knowledge_base_id: int
    enabled: bool
    effective_from: Literal["next_turn"] = "next_turn"
    warning: Literal["no_ready_files"] | None = None


class RuntimePersonalKnowledgeCapabilities(BaseModel):
    rag: bool = True
    wiki: bool = False


class RuntimePersonalKnowledgeItem(BaseModel):
    id: int
    name: str
    description: str | None = None
    ready_file_count: int
    capabilities: RuntimePersonalKnowledgeCapabilities = Field(
        default_factory=RuntimePersonalKnowledgeCapabilities
    )


class RuntimePersonalKnowledgeState(BaseModel):
    enabled: bool
    knowledge_bases: list[RuntimePersonalKnowledgeItem]
    revision: str


class RuntimePersonalKnowledgeSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4000)
    knowledge_base_ids: list[int] | None = None


class RuntimePersonalKnowledgeMatch(BaseModel):
    knowledge_base_id: int
    knowledge_base_name: str
    file_id: int
    filename: str
    chunk_id: str | None = None
    content: str
    score: float | None = None
    page: int | None = None
    source_url: str
    image_source_url: str | None = None


class RuntimePersonalKnowledgeSearchResponse(BaseModel):
    query: str
    matches: list[RuntimePersonalKnowledgeMatch]
    low_confidence: bool
    searched_knowledge_base_ids: list[int]


class RuntimePersonalKnowledgeSourceFile(BaseModel):
    file_id: int
    filename: str
    source_url: str


class RuntimePersonalKnowledgeWikiMatch(BaseModel):
    source_type: Literal["wiki_page"] = "wiki_page"
    knowledge_base_id: int
    knowledge_base_name: str
    slug: str
    title: str
    summary: str | None = None
    match_snippet: str | None = None
    source_url: str
    source_files: list[RuntimePersonalKnowledgeSourceFile]


class RuntimePersonalKnowledgeWikiSearchResponse(BaseModel):
    query: str
    matches: list[RuntimePersonalKnowledgeWikiMatch]
    low_confidence: bool
    searched_knowledge_base_ids: list[int]


class RuntimePersonalKnowledgeHybridSearchResponse(BaseModel):
    query: str
    rag_results: list[RuntimePersonalKnowledgeMatch]
    wiki_results: list[RuntimePersonalKnowledgeWikiMatch]
    rag_searched_knowledge_base_ids: list[int]
    wiki_searched_knowledge_base_ids: list[int]
    low_confidence: bool


class RuntimePersonalKnowledgeWikiReadPageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge_base_id: int
    slug: str = Field(min_length=1, max_length=512)


class RuntimePersonalKnowledgeWikiPage(BaseModel):
    source_type: Literal["wiki_page"] = "wiki_page"
    knowledge_base_id: int
    knowledge_base_name: str
    slug: str
    title: str
    content: str
    source_url: str
    source_files: list[RuntimePersonalKnowledgeSourceFile]
