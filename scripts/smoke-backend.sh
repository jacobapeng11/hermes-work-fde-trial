#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
VENV_DIR="$BACKEND_DIR/.venv"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Gate 类型: environment" >&2
  echo "阻塞项: backend/.venv 不存在或不可执行。" >&2
  echo "建议配置: 先运行 bash scripts/init-db.sh 或手工创建 venv 并安装 backend/requirements.txt。" >&2
  exit 2
fi

(
  # Keep pytest on its default dev/runtime configuration while using the local
  # database credentials required by the chat-history integration tests.
  # shellcheck disable=SC1090
  source <(grep -E '^(MYSQL_HOST|MYSQL_PORT|MYSQL_USER|MYSQL_PASSWORD|MYSQL_DB|MYSQL_SSL_MODE)=' "$BACKEND_DIR/.env")
  export MYSQL_HOST MYSQL_PORT MYSQL_USER MYSQL_PASSWORD MYSQL_DB MYSQL_SSL_MODE
  cd "$ROOT_DIR"
  "$VENV_DIR/bin/python" -m pytest backend/tests
)

(
  cd "$BACKEND_DIR"
  "$VENV_DIR/bin/python" - <<'PY'
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
for path in ("/health", "/api/v1/health"):
    response = client.get(path)
    response.raise_for_status()
    payload = response.json()
    assert payload["status"] == "ok", payload
    print(f"{path}: ok")
PY
)
