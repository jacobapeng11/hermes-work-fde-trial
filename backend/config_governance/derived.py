from __future__ import annotations

from functools import cached_property

from app.hermes_runtime.constants import append_url_path


class DerivedSettings:
    @cached_property
    def skillhub_category_mapping(self) -> dict[str, str]:
        import json

        return dict(json.loads(self.SKILLHUB_CATEGORY_MAPPING_JSON))

    @cached_property
    def skillhub_allowed_feeds(self) -> frozenset[str]:
        return frozenset(
            item.strip() for item in self.SKILLHUB_ALLOWED_FEEDS.split(",") if item.strip()
        )

    @cached_property
    def database_url(self) -> str:
        return (
            "mysql+asyncmy://"
            f"{self.MYSQL_USER}:{self.MYSQL_PASSWORD}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DB}"
            "?charset=utf8mb4"
        )

    @cached_property
    def platform_api_base_url(self) -> str:
        return append_url_path(self.PUBLIC_API_BASE, "/api/v1")

    @cached_property
    def runtime_internal_base_url(self) -> str:
        return self.PUBLIC_API_BASE.rstrip("/")

    @cached_property
    def office_preview_jwt_secret(self) -> str:
        if self.ONLYOFFICE_JWT_SECRET:
            return self.ONLYOFFICE_JWT_SECRET
        return self.RUNTIME_TOKEN_SECRET if self.APP_ENV.lower() != "production" else ""

    @cached_property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]
