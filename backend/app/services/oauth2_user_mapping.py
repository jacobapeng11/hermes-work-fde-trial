from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from app.services.oauth2 import OAuth2Error


@dataclass(frozen=True)
class ExternalIdentity:
    provider_id: str
    subject: str
    work_no: str
    display_name: str | None = None
    email: str | None = None


def map_identity(config: Any, info: dict) -> ExternalIdentity:
    def field(path: str) -> Any:
        value = info
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        return value

    subject, work_no = field(config.OAUTH2_USER_ID_FIELD), field(config.OAUTH2_USER_WORK_NO_FIELD)
    if type(subject) is int:
        subject = str(subject)
    if (
        not isinstance(subject, str)
        or not subject.strip()
        or len(subject) > 255
        or any(unicodedata.category(c).startswith("C") for c in subject)
        or not isinstance(work_no, str)
        or not work_no.strip()
        or len(work_no.strip()) > 32
        or any(unicodedata.category(c).startswith("C") for c in work_no)
    ):
        raise OAuth2Error("oauth2_identity_invalid", "认证身份或可信工号无效", 401)

    def optional_text(path: str, limit: int) -> str | None:
        value = field(path) if path else None
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.strip()) > limit
            or any(unicodedata.category(c).startswith("C") for c in value)
            or any(
                marker in value for marker in ("PLATFORM_IDENTITY_BEGIN", "PLATFORM_IDENTITY_END")
            )
        ):
            return None
        return value.strip()

    return ExternalIdentity(
        config.OAUTH2_PROVIDER_ID,
        subject,
        work_no.strip(),
        optional_text(config.OAUTH2_USER_NAME_FIELD, 100),
        optional_text(config.OAUTH2_USER_EMAIL_FIELD, 200),
    )
