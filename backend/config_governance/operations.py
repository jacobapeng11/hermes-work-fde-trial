from __future__ import annotations

from pydantic import Field


class OperationsSettings:
    # Hermes starts its self-improvement review asynchronously after a user
    # turn has completed. These bounds authorize only that short follow-up
    # activity without keeping the user-facing run active.
    POST_TURN_REVIEW_ACTIVATION_WINDOW_SECONDS: int = Field(default=30, ge=1)
    POST_TURN_REVIEW_IDLE_TIMEOUT_SECONDS: int = Field(default=15, ge=1)
    POST_TURN_REVIEW_MAX_DURATION_SECONDS: int = Field(default=300, ge=1)
    SCHEDULED_TASK_SCAN_INTERVAL_SECONDS: float = Field(default=10.0, ge=0.1)
    SCHEDULED_TASK_SESSION_RETRY_INTERVAL_SECONDS: float = Field(default=2.0, ge=0.1)
    SCHEDULED_TASK_SESSION_ADMISSION_TIMEOUT_SECONDS: float = Field(default=60.0, ge=1.0)
    SCHEDULED_TASK_EXECUTION_LEASE_SECONDS: int = Field(default=90, ge=30, le=3600)
    SCHEDULED_TASK_EXECUTION_HEARTBEAT_SECONDS: float = Field(default=20.0, ge=1.0, le=300.0)
    SCHEDULED_TASK_SCAN_BATCH_SIZE: int = Field(default=100, ge=1, le=1000)
    CHAT_FILE_MAX_UPLOAD_MB: int = 100
    CHAT_FILE_ALLOWED_ROOT: str = "workspace"
    FILE_DOWNLOAD_SIGNING_SECRET: str = ""
    FILE_DOWNLOAD_URL_TTL_SECONDS: int = Field(
        default=86_400,
        ge=300,
        le=604_800,
    )
