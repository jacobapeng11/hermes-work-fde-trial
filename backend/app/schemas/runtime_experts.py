from __future__ import annotations

from pydantic import BaseModel, Field


class RuntimeExpertCapabilities(BaseModel):
    skills: list[str] = Field(default_factory=list)
    mcps: list[str] = Field(default_factory=list)
    knowledge_bases: list[str] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)


class RuntimeExpertRosterItem(BaseModel):
    display_name: str
    assignee: str
    description: str = ""
    capabilities: RuntimeExpertCapabilities


class RuntimeExpertRosterResponse(BaseModel):
    experts: list[RuntimeExpertRosterItem] = Field(default_factory=list)
