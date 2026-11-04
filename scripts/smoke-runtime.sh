#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
VENV_DIR="$BACKEND_DIR/.venv"
: "${HERMES_RUNTIME_IMAGE:?HERMES_RUNTIME_IMAGE must come from backend/.env}"
RUNTIME_IMAGE="${HERMES_RUNTIME_IMAGE}"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Gate 类型: environment" >&2
  echo "阻塞项: backend/.venv 不存在。" >&2
  echo "影响范围: 无法执行 runtime smoke 前置测试。" >&2
  exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Gate 类型: environment" >&2
  echo "阻塞项: docker 不可用，无法验证统一 Runtime 镜像。" >&2
  exit 2
fi

if ! docker image inspect "$RUNTIME_IMAGE" >/dev/null 2>&1; then
  echo "Gate 类型: runtime-image" >&2
  echo "阻塞项: 统一 Runtime 镜像不存在: $RUNTIME_IMAGE" >&2
  exit 2
fi

docker run --rm --entrypoint /opt/hermes/.venv/bin/python "$RUNTIME_IMAGE" -c '
import importlib.metadata as metadata
import importlib.util
import os
from pathlib import Path

import hermes_cli.main
import platform_supervisor.main

required = ("hermes_cli.web_server", "gateway.run", "platform_supervisor.main")
missing = [name for name in required if importlib.util.find_spec(name) is None]
assert not missing, f"missing Runtime startup modules: {missing}"
assert metadata.version("hermes-platform-supervisor")
entrypoint = Path("/opt/hermes/.venv/bin/platform-supervisor")
assert entrypoint.is_file() and os.access(entrypoint, os.X_OK)
assert callable(platform_supervisor.main.run)
' >/dev/null
echo "unified runtime image entrypoints: ok image=$RUNTIME_IMAGE"

"$ROOT_DIR/scripts/smoke-runtime-network.sh"

(
  cd "$BACKEND_DIR"
  "$VENV_DIR/bin/python" - <<'PY'
from fastapi.testclient import TestClient
import os

from app.main import app

client = TestClient(app)
if os.getenv("HERMES_RUNTIME_WARMUP_MODE") == "real":
    warmup_response = client.post("/api/v1/user/runtime-workspace/warmup")
    warmup_response.raise_for_status()
    warmup_payload = warmup_response.json()
    assert warmup_payload["runtime_status"] == "ready", warmup_payload
else:
    response = client.get("/api/v1/user/runtime-workspace")
    response.raise_for_status()
    payload = response.json()
    assert "runtime_workspace_id" in payload, payload
    assert "runtime_status" in payload, payload
    print("runtime workspace API: ok")

    warmup_response = client.post("/api/v1/user/runtime-workspace/warmup")
    warmup_response.raise_for_status()
    warmup_payload = warmup_response.json()
    assert warmup_payload["runtime_workspace_id"] == payload["runtime_workspace_id"], warmup_payload
    assert warmup_payload["runtime_status"] in {"ready", "starting"}, warmup_payload
print("runtime workspace warmup API: ok")
PY
)
