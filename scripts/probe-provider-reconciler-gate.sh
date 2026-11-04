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
  echo "阻塞项: backend/.venv 不存在。" >&2
  exit 2
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Gate 类型: environment" >&2
  echo "阻塞项: backend/.env 不存在。" >&2
  echo "处理方式: 直接创建并配置 backend/.env" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
: "${SMOKE_USER_ID:?SMOKE_USER_ID must identify an existing smoke user}"
hermes_clear_proxy_env

(
  cd "$BACKEND_DIR"
  "$VENV_DIR/bin/python" - <<'PY'
from __future__ import annotations

import asyncio
import os
import json
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.models import CloudHermesProfile, HermesRuntimeBinding
from app.services.runtime_infrastructure import build_runtime_protocol_adapters
from app.services.runtime_workspace import RuntimeApiKeyCodec


REQUIRED_FLAGS = {
    "terminal_status_query": "按 hermes_run_id 查询权威 terminal status",
    "event_replay_cursor": "provider events 可按 cursor 续读",
    "event_idempotency": "provider events 有幂等键或稳定序号",
    "temporarily_unavailable_semantics": "定义暂时不可用与终态缺失的区别",
}


def _lookup(payload: dict[str, Any], key: str) -> Any:
    candidates = [
        payload.get(key),
        (payload.get("run_reconciliation") or {}).get(key) if isinstance(payload.get("run_reconciliation"), dict) else None,
        (payload.get("reconciliation") or {}).get(key) if isinstance(payload.get("reconciliation"), dict) else None,
        (payload.get("runs") or {}).get(key) if isinstance(payload.get("runs"), dict) else None,
    ]
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return None


def _is_enabled(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.lower() in {"true", "enabled", "supported", "stable", "authoritative"}
    return False


async def _endpoint() -> RuntimeEndpoint:
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(HermesRuntimeBinding)
                .join(CloudHermesProfile, CloudHermesProfile.id == HermesRuntimeBinding.runtime_workspace_id)
                .where(
                    CloudHermesProfile.org_id == settings.DEFAULT_ORG_ID,
                    CloudHermesProfile.user_id == int(os.environ["SMOKE_USER_ID"]),
                    CloudHermesProfile.profile_type == "default_chat_agent",
                    CloudHermesProfile.status == "active",
                    HermesRuntimeBinding.status == "ready",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise SystemExit(
                "Gate 类型: environment\n"
                "阻塞项: ready runtime binding missing; run HERMES_RUNTIME_WARMUP_MODE=real bash scripts/smoke-runtime.sh"
            )
        if not row.endpoint_base_url or not row.endpoint_host_header:
            raise SystemExit("Gate 类型: environment\n阻塞项: runtime Dashboard endpoint missing")
        return RuntimeEndpoint(
            runtime_workspace_id=int(row.runtime_workspace_id),
            runtime_id=str(row.runtime_id),
            endpoint_base_url=str(row.endpoint_base_url),
            endpoint_host_header=str(row.endpoint_host_header),
            api_key=RuntimeApiKeyCodec().decrypt(str(row.api_key_encrypted)),
        )


async def main() -> None:
    endpoint = await _endpoint()
    capabilities = await build_runtime_protocol_adapters().dashboard_adapter.capabilities(endpoint)
    missing = [description for flag, description in REQUIRED_FLAGS.items() if not _is_enabled(_lookup(capabilities, flag))]
    if missing:
        print("Gate 类型: contract")
        print("状态: pending")
        print("原因: runtime capabilities 未声明 provider 补偿型 reconciler 所需的权威契约")
        print("缺失:")
        for item in missing:
            print(f"- {item}")
        print("capabilities 摘要:")
        print(json.dumps(capabilities, ensure_ascii=False, sort_keys=True)[:2000])
        raise SystemExit(3)

    print("Gate 类型: contract")
    print("状态: passed")
    print("provider/history reconciler prerequisite contract: ok")


asyncio.run(main())
PY
)
