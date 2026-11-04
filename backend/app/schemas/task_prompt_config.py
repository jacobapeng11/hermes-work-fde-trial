from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TaskPromptTask(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    summary: str = Field(min_length=1, max_length=80)
    detail: str = Field(min_length=1, max_length=4000)


class TaskPromptTopic(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=32)
    tasks: list[TaskPromptTask] = Field(min_length=1, max_length=12)


class TaskPromptTab(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=24)
    description: str = Field(min_length=1, max_length=200)
    is_default: bool = False
    show_board_gallery: bool = False
    topics: list[TaskPromptTopic] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def require_unique_topic_titles(self) -> "TaskPromptTab":
        titles = [topic.title for topic in self.topics]
        if len(titles) != len(set(titles)):
            raise ValueError("topic titles must be unique in a tab")
        return self


class TaskPromptConfigDocument(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    tabs: list[TaskPromptTab] = Field(min_length=1, max_length=8)

    @field_validator("tabs")
    @classmethod
    def require_unique_tab_labels(cls, value: list[TaskPromptTab]) -> list[TaskPromptTab]:
        labels = [tab.label for tab in value]
        if len(labels) != len(set(labels)):
            raise ValueError("tab labels must be unique")
        return value

    @model_validator(mode="after")
    def require_single_default_tab(self) -> "TaskPromptConfigDocument":
        default_count = sum(1 for tab in self.tabs if tab.is_default)
        if default_count != 1:
            raise ValueError("exactly one default tab is required")
        return self


class TaskPromptConfigState(BaseModel):
    revision: int = 0
    config: TaskPromptConfigDocument | None = None


class TaskPromptConfigSaveRequest(BaseModel):
    revision: int = Field(ge=0)
    config: TaskPromptConfigDocument


class TaskPromptConfigSaveResponse(BaseModel):
    revision: int
    config: TaskPromptConfigDocument
