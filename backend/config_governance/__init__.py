from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from .application import ApplicationSettings
from .auth import AuthSettings
from .database import DatabaseSettings
from .derived import DerivedSettings
from .integrations import IntegrationSettings
from .operations import OperationsSettings
from .resources import ResourceSettings
from .runtime import RuntimeSettings
from .validation import ValidationSettings


class Settings(
    AuthSettings,
    ApplicationSettings,
    DatabaseSettings,
    RuntimeSettings,
    ResourceSettings,
    IntegrationSettings,
    OperationsSettings,
    ValidationSettings,
    DerivedSettings,
    BaseSettings,
):
    """兼容的统一 Settings 入口；字段按责任域声明在 config 子模块中。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
        hide_input_in_errors=True,
    )


settings = Settings()

__all__ = ["Settings", "settings"]
