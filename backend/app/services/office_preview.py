from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from app.services.chat_files import ChatFileRecord
from app.services.runtime_workspace import Principal


class OfficePreviewError(ValueError):
    pass


OFFICE_DOCUMENT_TYPES = {
    "doc": "word",
    "docm": "word",
    "docx": "word",
    "dot": "word",
    "dotm": "word",
    "dotx": "word",
    "odt": "word",
    "ott": "word",
    "rtf": "word",
    "xls": "cell",
    "xlsb": "cell",
    "xlsm": "cell",
    "xlsx": "cell",
    "xlt": "cell",
    "xltm": "cell",
    "xltx": "cell",
    "ods": "cell",
    "ots": "cell",
    "ppt": "slide",
    "pptm": "slide",
    "pptx": "slide",
    "pps": "slide",
    "ppsm": "slide",
    "ppsx": "slide",
    "pot": "slide",
    "potm": "slide",
    "potx": "slide",
    "odp": "slide",
    "otp": "slide",
}


def office_file_type(filename: str) -> str:
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in OFFICE_DOCUMENT_TYPES:
        raise OfficePreviewError("file type is not supported by Office preview")
    return extension


def create_office_file_token(
    *,
    principal: Principal,
    file_id: int,
    secret: str,
    ttl_seconds: int,
    now: int | None = None,
) -> str:
    issued_at = int(time.time()) if now is None else now
    return encode_hs256(
        {
            "token_type": "office_file",
            "org_id": principal.org_id,
            "user_id": principal.user_id,
            "file_id": file_id,
            "iat": issued_at,
            "exp": issued_at + ttl_seconds,
        },
        secret,
    )


def decode_office_file_token(token: str, *, secret: str, now: int | None = None) -> tuple[Principal, int]:
    payload = decode_hs256(token, secret=secret)
    if payload.get("token_type") != "office_file":
        raise OfficePreviewError("invalid Office preview token")
    current_time = int(time.time()) if now is None else now
    if int(payload.get("exp", 0)) < current_time:
        raise OfficePreviewError("expired Office preview token")
    try:
        principal = Principal(org_id=int(payload["org_id"]), user_id=int(payload["user_id"]))
        file_id = int(payload["file_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OfficePreviewError("invalid Office preview token") from exc
    return principal, file_id


def create_personal_knowledge_office_token(
    *,
    principal: Principal,
    knowledge_base_id: int,
    file_id: int,
    secret: str,
    ttl_seconds: int,
    now: int | None = None,
) -> str:
    issued_at = int(time.time()) if now is None else now
    return encode_hs256(
        {
            "token_type": "personal_knowledge_office_file",
            "org_id": principal.org_id,
            "user_id": principal.user_id,
            "knowledge_base_id": knowledge_base_id,
            "file_id": file_id,
            "iat": issued_at,
            "exp": issued_at + ttl_seconds,
        },
        secret,
    )


def decode_personal_knowledge_office_token(
    token: str,
    *,
    secret: str,
    now: int | None = None,
) -> tuple[Principal, int, int]:
    payload = decode_hs256(token, secret=secret)
    if payload.get("token_type") != "personal_knowledge_office_file":
        raise OfficePreviewError("invalid Office preview token")
    current_time = int(time.time()) if now is None else now
    if int(payload.get("exp", 0)) < current_time:
        raise OfficePreviewError("expired Office preview token")
    try:
        principal = Principal(org_id=int(payload["org_id"]), user_id=int(payload["user_id"]))
        knowledge_base_id = int(payload["knowledge_base_id"])
        file_id = int(payload["file_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OfficePreviewError("invalid Office preview token") from exc
    return principal, knowledge_base_id, file_id


def build_office_config(
    *,
    record: ChatFileRecord,
    content_url: str,
    secret: str,
    user_id: int,
    ttl_seconds: int,
    allow_download: bool = True,
    now: int | None = None,
) -> dict[str, Any]:
    file_type = office_file_type(record.display_name)
    document_type = OFFICE_DOCUMENT_TYPES[file_type]
    version_source = f"{record.file_id}:{record.relative_path}:{record.size_bytes}:{record.created_at or ''}"
    document_key = hashlib.sha256(version_source.encode("utf-8")).hexdigest()[:40]
    config: dict[str, Any] = {
        "document": {
            "fileType": file_type,
            "key": document_key,
            "title": record.display_name,
            "url": content_url,
            "permissions": {
                "chat": False,
                "comment": False,
                "copy": allow_download,
                "download": allow_download,
                "edit": False,
                "fillForms": False,
                "modifyContentControl": False,
                "print": allow_download,
                "review": False,
            },
        },
        "documentType": document_type,
        "editorConfig": {
            "coEditing": {"mode": "strict", "change": False},
            "lang": "zh",
            "mode": "view",
            "user": {"id": str(user_id), "name": "Hermes User"},
            "customization": {
                "comments": False,
                "compactHeader": True,
                "compactToolbar": True,
                "help": False,
            },
        },
        "type": "desktop",
    }
    issued_at = int(time.time()) if now is None else now
    config["token"] = encode_hs256({**config, "iat": issued_at, "exp": issued_at + ttl_seconds}, secret)
    return config


def encode_hs256(payload: dict[str, Any], secret: str) -> str:
    if not secret:
        raise OfficePreviewError("Office preview JWT secret is not configured")
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = f"{_b64json(header)}.{_b64json(payload)}"
    signature = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


def decode_hs256(token: str, *, secret: str) -> dict[str, Any]:
    if not secret:
        raise OfficePreviewError("Office preview JWT secret is not configured")
    try:
        header_part, payload_part, signature_part = token.split(".", 2)
        header = json.loads(_b64decode(header_part))
        payload = json.loads(_b64decode(payload_part))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise OfficePreviewError("invalid Office preview token") from exc
    if header.get("alg") != "HS256" or not isinstance(payload, dict):
        raise OfficePreviewError("invalid Office preview token")
    signing_input = f"{header_part}.{payload_part}"
    expected = _b64(hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature_part, expected):
        raise OfficePreviewError("invalid Office preview token")
    return payload


def _b64json(value: dict[str, Any]) -> str:
    return _b64(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> str:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
