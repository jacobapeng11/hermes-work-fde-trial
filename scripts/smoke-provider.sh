#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/backend/.env"

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
hermes_clear_proxy_env

base_url="${OPENAI_COMPATIBLE_BASE_URL:-${SEED_MODEL_API_ENDPOINT:-}}"
api_key="${OPENAI_COMPATIBLE_API_KEY:-${MODEL_PROVIDER_TEST_API_KEY:-}}"
model_key="${OPENAI_COMPATIBLE_MODEL:-${SEED_MODEL_KEY:-}}"

if [[ -z "$base_url" || -z "$api_key" || -z "$model_key" ]]; then
  echo "Gate 类型: provider" >&2
  echo "阻塞项: 缺少 OPENAI_COMPATIBLE_BASE_URL、OPENAI_COMPATIBLE_API_KEY 或 OPENAI_COMPATIBLE_MODEL。" >&2
  exit 2
fi

body_file="$(mktemp)"
cleanup() {
  rm -f "$body_file"
}
trap cleanup EXIT

base_url="${base_url%/}"
no_proxy="${PROVIDER_SMOKE_NO_PROXY:-*}"
status="$(
  curl --noproxy "$no_proxy" -sS -m "${PROVIDER_SMOKE_TIMEOUT_SECONDS:-20}" \
    -o "$body_file" \
    -w "%{http_code}" \
    -H "Authorization: Bearer $api_key" \
    "$base_url/models" || true
)"

if [[ "$status" != 2* ]]; then
  echo "provider models smoke: failed status=$status" >&2
  sed -E 's/(sk-[A-Za-z0-9_-]{8,})/[redacted]/g; s/(Bearer )[A-Za-z0-9._~+\/-]+/\1[redacted]/g' "$body_file" >&2 || true
  exit 1
fi

"$ROOT_DIR/backend/.venv/bin/python" - "$body_file" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
items = payload.get("data") if isinstance(payload, dict) else None
if not isinstance(items, list):
    raise SystemExit("provider models smoke: response does not contain a data list")
print(f"provider models smoke: ok model_count={len(items)}")
PY
