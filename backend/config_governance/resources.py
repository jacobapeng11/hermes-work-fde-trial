from __future__ import annotations

from pydantic import Field


class ResourceSettings:
    APPLICATION_AGENT_CPU_REQUEST: str = Field(default="1m", min_length=1, max_length=32)
    APPLICATION_AGENT_MEMORY_REQUEST: str = Field(default="200Mi", min_length=1, max_length=32)
    PERSONAL_RUNTIME_CPU_REQUEST: str = Field(default="1m", min_length=1, max_length=32)
    PERSONAL_RUNTIME_MEMORY_REQUEST: str = Field(default="200Mi", min_length=1, max_length=32)
