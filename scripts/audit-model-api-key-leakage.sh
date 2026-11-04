#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
VENV_DIR="$BACKEND_DIR/.venv"
ENV_FILE="$BACKEND_DIR/.env"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Gate 类型: environment" >&2
  echo "阻塞项: backend/.venv 不存在或不可执行。" >&2
  exit 2
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Gate 类型: environment" >&2
  echo "阻塞项: backend/.env 不存在。" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
hermes_clear_proxy_env

(
  cd "$BACKEND_DIR"
  "$VENV_DIR/bin/python" - <<'PY'
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from redis.asyncio import from_url
from sqlalchemy import select

from app.api.v1.admin.model_config import _catalog_snapshot_response
from app.config import settings
from app.database import AsyncSessionLocal, engine
from app.models import RuntimeAuditLog
from app.schemas.admin import AdminModelConfigEntrySummary
from app.services.admin_auth import AdminPrincipal
from app.services.admin_model_catalog import (
    AdminModelCatalogService,
    AdminModelConfigItem,
    InMemoryAdminModelCatalogStore,
)

ROOT_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIRS = [ROOT_DIR / "frontend" / "admin-web", ROOT_DIR / "frontend" / "user-web"]
SCHEMA_FILE = ROOT_DIR / "packages" / "schemas" / "src" / "index.ts"

FORBIDDEN_RESPONSE_FIELDS = {
    "api_key",
    "apiKey",
    "api_key_encrypted",
    "apiKeyEncrypted",
    "provider_api_key",
    "providerApiKey",
    "runtime_token",
    "runtimeToken",
}
FORBIDDEN_AUDIT_KEYS = {
    "api_key",
    "api_key_encrypted",
    "provider_api_key",
    "runtime_token",
    "runtime_api_key",
}
FORBIDDEN_REDIS_PLAINTEXT_KEYS = {
    "api_key",
    "apiKey",
    "provider_api_key",
    "providerApiKey",
    "runtime_token",
    "runtimeToken",
    "runtime_api_key",
    "runtimeApiKey",
}
STORAGE_MARKERS = ("localStorage.setItem", "sessionStorage.setItem")
SECRET_FIELD_MARKERS = ("apiKey", "api_key", "providerApiKey", "provider_api_key")


def candidate_secrets() -> list[str]:
    values = [
        os.environ.get("OPENAI_COMPATIBLE_API_KEY", ""),
        os.environ.get("MODEL_PROVIDER_TEST_API_KEY", ""),
        os.environ.get("SEED_MODEL_API_KEY_ENCRYPTED", ""),
    ]
    return [value for value in {item.strip() for item in values} if len(value) >= 8]


def assert_no_secret_values(payload: str, *, location: str, secrets: list[str]) -> None:
    for secret in secrets:
        if secret and secret in payload:
            raise SystemExit(f"api key leakage audit: secret value leaked in {location}")


def walk_json(value: Any, *, location: str, forbidden_keys: set[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) in forbidden_keys:
                raise SystemExit(f"api key leakage audit: forbidden key {key!r} in {location}")
            walk_json(child, location=location, forbidden_keys=forbidden_keys)
    elif isinstance(value, list):
        for child in value:
            walk_json(child, location=location, forbidden_keys=forbidden_keys)


async def assert_admin_model_response_safe(secrets: list[str]) -> None:
    fields = set(AdminModelConfigEntrySummary.model_fields)
    forbidden = fields & FORBIDDEN_RESPONSE_FIELDS
    if forbidden:
        raise SystemExit(f"api key leakage audit: admin response schema exposes {sorted(forbidden)}")

    service = AdminModelCatalogService(InMemoryAdminModelCatalogStore())
    principal = AdminPrincipal(org_id=1, user_id=7, roles=("admin",))
    await service.publish_catalog(
        principal=principal,
        entries=[
            AdminModelConfigItem(
                provider_key="audit-provider",
                provider_display_name="Audit Provider",
                provider_type="openai_compatible",
                api_endpoint="https://provider.example/v1",
                api_key="audit-secret-value",
                model_key="audit-model",
                model_display_name="Audit Model",
                context_length=8192,
                tags=["audit"],
                is_restricted=False,
            )
        ],
    )
    response = _catalog_snapshot_response(await service.list_catalog(principal=principal))
    payload = response.model_dump(by_alias=True, mode="json")
    serialized = json.dumps(payload, ensure_ascii=False)
    walk_json(payload, location="admin model config response", forbidden_keys=FORBIDDEN_RESPONSE_FIELDS)
    assert_no_secret_values(serialized, location="admin model config response", secrets=secrets + ["audit-secret-value"])
    if "credentialConfigured" not in serialized and "credential_configured" not in serialized:
        raise SystemExit("api key leakage audit: configured-state field missing from admin response")


def assert_typescript_contract_safe() -> None:
    text = SCHEMA_FILE.read_text(encoding="utf-8")
    marker = "export interface AdminModelConfigEntrySummary"
    start = text.find(marker)
    if start < 0:
        raise SystemExit("api key leakage audit: AdminModelConfigEntrySummary missing")
    end = text.find("\n}", start)
    block = text[start:end]
    if "credentialConfigured" not in block:
        raise SystemExit("api key leakage audit: credentialConfigured missing from TS summary")
    forbidden = [field for field in ("apiKey?:", "apiKeyEncrypted", "providerApiKey", "runtimeToken") if field in block]
    if forbidden:
        raise SystemExit(f"api key leakage audit: TS summary exposes {forbidden}")


def assert_frontend_storage_safe() -> int:
    checked = 0
    for root in FRONTEND_DIRS:
        for path in root.rglob("*"):
            if any(part in {"dist", "node_modules", "coverage"} for part in path.parts):
                continue
            if path.suffix not in {".ts", ".tsx", ".js", ".jsx"}:
                continue
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if any(marker in line for marker in STORAGE_MARKERS):
                    checked += 1
                    if any(marker in line for marker in SECRET_FIELD_MARKERS):
                        rel = path.relative_to(ROOT_DIR)
                        raise SystemExit(
                            "api key leakage audit: model API key is written to frontend storage "
                            f"at {rel}:{lineno}"
                        )
    return checked


async def assert_redis_cache_safe(secrets: list[str]) -> int:
    redis = from_url(settings.REDIS_URL, decode_responses=True)
    checked = 0
    try:
        cursor: int | str = 0
        while True:
            cursor, keys = await redis.scan(cursor=cursor, match="hermes:model:*", count=200)
            for key in keys:
                key_str = str(key)
                if ":lock:" in key_str or ":revision:" in key_str:
                    continue
                payload = await redis.get(key_str)
                if payload is None:
                    continue
                checked += 1
                assert_no_secret_values(payload, location=f"redis {key_str}", secrets=secrets)
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                walk_json(parsed, location=f"redis {key_str}", forbidden_keys=FORBIDDEN_REDIS_PLAINTEXT_KEYS)
            if int(cursor) == 0:
                break
    finally:
        await redis.aclose()
    return checked


async def assert_audit_logs_safe(secrets: list[str]) -> int:
    checked = 0
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(RuntimeAuditLog).order_by(RuntimeAuditLog.created_at.desc(), RuntimeAuditLog.id.desc()).limit(200)
        )
        for row in result.scalars().all():
            checked += 1
            detail = dict(row.detail or {})
            walk_json(detail, location=f"audit log {row.id}", forbidden_keys=FORBIDDEN_AUDIT_KEYS)
            assert_no_secret_values(
                json.dumps(detail, ensure_ascii=False),
                location=f"audit log {row.id}",
                secrets=secrets,
            )
    return checked


async def main() -> None:
    secrets = candidate_secrets()
    await assert_admin_model_response_safe(secrets)
    assert_typescript_contract_safe()
    frontend_storage_writes = assert_frontend_storage_safe()
    redis_payloads = await assert_redis_cache_safe(secrets)
    audit_logs = await assert_audit_logs_safe(secrets)
    await engine.dispose()
    print(
        "api key leakage audit: ok "
        f"frontend_storage_writes={frontend_storage_writes} "
        f"redis_payloads={redis_payloads} "
        f"audit_logs={audit_logs}"
    )


asyncio.run(main())
PY
)
