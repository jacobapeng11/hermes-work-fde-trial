#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
USER_WEB_DIR="$ROOT_DIR/frontend/user-web"
ADMIN_WEB_DIR="$ROOT_DIR/frontend/admin-web"
ENV_FILE="$BACKEND_DIR/.env"
BACKEND_RUNTIME_DIR="$ROOT_DIR/.runtime/public-dev"
FRONTEND_RUNTIME_DIR="$ROOT_DIR/.runtime/public-frontends-dev"

# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib/public-dev-lifecycle.sh"

overall_status=0
env_loaded=0
BACKEND_HOST=""
BACKEND_LOCAL_HOST=""
USER_WEB_HOST=""
ADMIN_WEB_HOST=""

echo "Hermes Cloud Platform status"
echo "root: $ROOT_DIR"

if [[ -f "$ENV_FILE" ]]; then
  echo "backend env: present"
  if public_dev_load_backend_env \
    && public_dev_resolve_ports; then
    env_loaded=1
  else
    echo "backend config: not_ready"
    overall_status=1
  fi
else
  echo "backend env: missing"
  overall_status=1
fi

if [[ -x "$BACKEND_DIR/.venv/bin/python" ]]; then
  echo "backend venv: present"
else
  echo "backend venv: missing"
  overall_status=1
fi

BACKEND_PORT="${BACKEND_PORT:-}"
USER_WEB_PORT="${USER_WEB_PORT:-}"
ADMIN_WEB_PORT="${ADMIN_WEB_PORT:-}"

if command -v mysql >/dev/null 2>&1 && [[ "$env_loaded" == "1" ]]; then
  if [[ -n "${MYSQL_HOST:-}" && -n "${MYSQL_PORT:-}" && -n "${MYSQL_USER:-}" && -n "${MYSQL_PASSWORD:-}" ]] \
    && mysql --ssl-mode="${MYSQL_SSL_MODE:-DISABLED}" -h"$MYSQL_HOST" -P"$MYSQL_PORT" -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" --connect-timeout=2 -e "SELECT 1" >/dev/null 2>&1; then
    echo "mysql: authenticated"
  else
    echo "mysql: not_ready"
    overall_status=1
  fi
else
  echo "mysql: not_ready (client or configuration unavailable)"
  overall_status=1
fi

check_service() {
  local service="$1"
  local service_dir="$2"
  local port="$3"
  local url="$4"
  local runtime_dir="$BACKEND_RUNTIME_DIR"
  if [[ "$service" == "user-web" || "$service" == "admin-web" ]]; then
    runtime_dir="$FRONTEND_RUNTIME_DIR"
  fi
  local pid_file="$runtime_dir/$service.pid"
  local pid=""

  if [[ -f "$pid_file" ]]; then
    pid="$(tr -d '[:space:]' < "$pid_file")"
  fi
  if [[ -n "$pid" ]] && public_dev_process_belongs "$pid" "$service" "$service_dir" "$port"; then
    echo "$service process: ready (pid=$pid)"
  else
    echo "$service process: not_ready"
    overall_status=1
  fi

  if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
    echo "$service http: ready ($url)"
  else
    echo "$service http: not_ready ($url)"
    overall_status=1
  fi
}

if [[ -n "${ENTERPRISE_KNOWLEDGE_HOST_PATH:-}" \
  && -f "$ENTERPRISE_KNOWLEDGE_HOST_PATH/PRIMER.md" \
  && -f "$ENTERPRISE_KNOWLEDGE_HOST_PATH/workflows/query.md" ]]; then
  echo "enterprise knowledge bundle: ready ($ENTERPRISE_KNOWLEDGE_HOST_PATH)"
else
  echo "enterprise knowledge bundle: not_ready"
  overall_status=1
fi
if "$ROOT_DIR/scripts/check-enterprise-knowledge-data-service.sh" \
  --backend-env "$ENV_FILE" >/dev/null 2>&1; then
  echo "enterprise data service: ready (systemd)"
else
  echo "enterprise data service: not_ready (systemd)"
  overall_status=1
fi

check_service "backend" "$BACKEND_DIR" "$BACKEND_PORT" \
  "http://${BACKEND_LOCAL_HOST}:${BACKEND_PORT}/api/v1/health"
check_service "user-web" "$USER_WEB_DIR" "$USER_WEB_PORT" \
  "http://${USER_WEB_HOST}:${USER_WEB_PORT}/user/chat"
check_service "admin-web" "$ADMIN_WEB_DIR" "$ADMIN_WEB_PORT" \
  "http://${ADMIN_WEB_HOST}:${ADMIN_WEB_PORT}/admin/"

if [[ "$env_loaded" == "1" ]]; then
  if ! python3 "$ROOT_DIR/scripts/lib/runtime_infra_probe.py" --format text; then
    overall_status=1
  fi
else
  echo "runtime_infrastructure: not_ready (backend configuration unavailable)"
  overall_status=1
fi

if [[ "$overall_status" == "0" ]]; then
  echo "overall: ready"
else
  echo "overall: not_ready"
fi
exit "$overall_status"
