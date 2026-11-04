from __future__ import annotations

from pydantic import Field


class IntegrationSettings:
    # SkillHub is deliberately a server-side integration. Every package is
    # copied into the owner's Runtime volume; this is never a shared cache.
    SKILLHUB_ENABLED: bool = True
    SKILLHUB_API_BASE_URL: str = "https://api.skillhub.cn"
    SKILLHUB_PROXY_URL: str = ""
    SKILLHUB_PROXY_CONNECT_IP: str = ""
    SKILLHUB_ALLOWED_FEEDS: str = "featured,hot,trending"
    SKILLHUB_DEFAULT_PAGE_SIZE: int = Field(default=12, ge=1, le=20)
    SKILLHUB_MAX_PAGE_SIZE: int = Field(default=20, ge=1, le=50)
    SKILLHUB_MIN_DOWNLOADS: int = Field(default=1000, ge=0)
    SKILLHUB_MIN_INSTALLS: int = Field(default=100, ge=0)
    SKILLHUB_CATEGORY_MAPPING_JSON: str = (
        '{"knowledge-management":"research","software-development":"software-development",'
        '"data-analysis":"data-science","design":"creative","ai-agent":"agent-automation"}'
    )
    SKILLHUB_VERSION_CHECK_TTL_SECONDS: int = Field(default=21600, ge=60)
    SKILLHUB_REQUEST_TIMEOUT_SECONDS: int = Field(default=10, ge=1, le=60)
    SKILLHUB_MAX_PACKAGE_BYTES: int = Field(default=25 * 1024 * 1024, ge=1)
    SKILLHUB_MAX_UNPACKED_BYTES: int = Field(default=100 * 1024 * 1024, ge=1)
    SKILLHUB_MAX_FILE_COUNT: int = Field(default=500, ge=1)
    SKILLHUB_MAX_SINGLE_FILE_BYTES: int = Field(default=25 * 1024 * 1024, ge=1)
    SKILLHUB_SIGNATURE_REQUIRED: bool = True
    APP_CENTER_MAX_PACKAGE_BYTES: int = Field(default=100 * 1024 * 1024, ge=1)
    APP_CENTER_MAX_APPS_PER_USER: int = Field(default=100, ge=1, le=1000)
    PERSONAL_DEFAULT_MCP_SLUG: str = "web-search-mcp"
    PERSONAL_DEFAULT_MCP_RETRY_SECONDS: float = Field(default=15.0, ge=1.0, le=300.0)
    PROFILE_CONVERGENCE_POLL_SECONDS: float = Field(default=1.0, ge=0.1, le=60.0)
    PROFILE_CONVERGENCE_LEASE_SECONDS: int = Field(default=180, ge=30, le=900)
    PROFILE_CONVERGENCE_CONCURRENCY: int = Field(default=4, ge=1, le=32)
    PROFILE_CONVERGENCE_BATCH_SIZE: int = Field(default=16, ge=1, le=256)
    MCP_REGISTRY_CONVERGENCE_POLL_SECONDS: float = Field(default=0.5, ge=0.1, le=60.0)
    MCP_REGISTRY_CONVERGENCE_LEASE_SECONDS: int = Field(default=180, ge=30, le=900)
    MCP_REGISTRY_CONVERGENCE_CONCURRENCY: int = Field(default=2, ge=1, le=16)
    MCP_REGISTRY_CONVERGENCE_BATCH_SIZE: int = Field(default=8, ge=1, le=128)
    RUNTIME_KANBAN_LEASE_RECONCILE_INTERVAL_SECONDS: int = Field(default=30, ge=5, le=300)
    RUNTIME_KANBAN_TEMPORARY_LEASE_SECONDS: int = Field(default=120, ge=15, le=600)
    RUNTIME_KANBAN_LEASE_RELEASE_GRACE_SECONDS: int = Field(default=60, ge=0, le=900)
    EXPERT_COLLABORATION_RECONCILE_INTERVAL_SECONDS: float = Field(
        default=2.0,
        ge=0.5,
        le=30.0,
    )
    EXPERT_COLLABORATION_LEASE_SECONDS: int = Field(default=300, ge=30, le=1800)
    EXPERT_COLLABORATION_MAX_SYNTHESIS_ATTEMPTS: int = Field(default=3, ge=1, le=10)
    EXPERT_COLLABORATION_SSE_POLL_SECONDS: float = Field(default=1.0, ge=0.25, le=10.0)
    EXPERT_RELEASE_ARTIFACT_RETENTION_DAYS: int = Field(default=30, ge=1)
    EXPERT_RELEASE_ARTIFACT_CLEANUP_INTERVAL_SECONDS: float = Field(default=3600.0, ge=60.0)
    ONLYOFFICE_PUBLIC_URL: str = ""
    ONLYOFFICE_INTERNAL_API_BASE: str = ""
    ONLYOFFICE_JWT_SECRET: str = ""
    ONLYOFFICE_PREVIEW_TOKEN_TTL_SECONDS: int = Field(default=300, ge=60, le=3600)
    KNOWLEDGE_ASSERTION_ISSUER: str = "hermes-work"
    KNOWLEDGE_ASSERTION_TTL_SECONDS: int = Field(default=60, ge=15, le=300)
    KNOWLEDGE_DATA_SERVICE_BASE_URL: str = ""
    KNOWLEDGE_DATA_SERVICE_TIMEOUT_SECONDS: int = Field(default=30, ge=1, le=120)
    KNOWLEDGE_SQL_QUERY_TIMEOUT_SECONDS: int = Field(default=70, ge=1, le=120)
    KNOWLEDGE_DATA_SERVICE_VERIFY_TLS: bool = True
    KNOWLEDGE_DATA_SERVICE_CA_FILE: str = ""
    DATA_SERVICE_ASSERTION_SECRET: str = ""
    DATA_SERVICE_ASSERTION_AUDIENCE: str = "enterprise-data"
    KNOWLEDGE_GOVERNANCE_SOURCE_ROOT: str = ""
    KNOWLEDGE_GOVERNANCE_COMPILE_TIMEOUT_SECONDS: int = Field(default=30, ge=5, le=120)
    KNOWLEDGE_GOVERNANCE_MAX_SNAPSHOT_BYTES: int = Field(
        default=8 * 1024 * 1024,
        ge=1024,
        le=64 * 1024 * 1024,
    )
    KNOWLEDGE_GOVERNANCE_MAX_NODES: int = Field(default=10_000, ge=1, le=100_000)
    KNOWLEDGE_GOVERNANCE_MAX_EDGES: int = Field(default=20_000, ge=1, le=200_000)
    KNOWLEDGE_DOCUMENT_SEARCH_URL: str = ""
    KNOWLEDGE_DOCUMENT_SERVICE_TIMEOUT_SECONDS: int = Field(default=30, ge=1, le=120)
    PERSONAL_KNOWLEDGE_PROVIDER: str = "filebase"
    PERSONAL_KNOWLEDGE_SYNC_BASE_URL: str = ""
    PERSONAL_KNOWLEDGE_SEARCH_URL: str = ""
    PERSONAL_KNOWLEDGE_MIN_SIMILARITY: float = Field(default=0.0, ge=0, le=1)
    PERSONAL_KNOWLEDGE_TIMEOUT_SECONDS: int = Field(default=30, ge=1, le=600)
    WEKNORA_API_BASE_URL: str = ""
    WEKNORA_API_KEY: str = ""
    WEKNORA_EMBEDDING_MODEL_ID: str = ""
    WEKNORA_SUMMARY_MODEL_ID: str = ""
    WEKNORA_WIKI_SYNTHESIS_MODEL_ID: str = ""
    WEKNORA_WIKI_EXTRACTION_GRANULARITY: str = "exhaustive"
    WEKNORA_TIMEOUT_SECONDS: int = Field(default=30, ge=1, le=600)
    PERSONAL_KNOWLEDGE_OSS_UPLOAD_URL: str = ""
    PERSONAL_KNOWLEDGE_OSS_BUCKET: str = "hermes-portal"
    PERSONAL_KNOWLEDGE_OSS_PUBLIC_BASE_URL: str = ""
    PERSONAL_KNOWLEDGE_OSS_INTERNAL_HOST: str = "storage.example.com"
    PERSONAL_KNOWLEDGE_MAX_UPLOAD_MB: int = Field(default=100, ge=1, le=100)
    AVATAR_OSS_UPLOAD_URL: str = ""
    AVATAR_OSS_BUCKET: str = "hermes-portal"
    AVATAR_OSS_PUBLIC_BASE_URL: str = ""
    AVATAR_OSS_INTERNAL_HOST: str = "storage.example.com"
    AVATAR_MAX_UPLOAD_MB: int = Field(default=2, ge=1, le=10)
    AVATAR_MAX_SOURCE_PIXELS: int = Field(default=16_777_216, ge=65_536)
