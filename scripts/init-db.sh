#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
ENV_FILE="$BACKEND_DIR/.env"
VENV_DIR="$BACKEND_DIR/.venv"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Gate 类型: environment" >&2
  echo "阻塞项: $ENV_FILE 不存在。" >&2
  echo "建议配置: 直接创建并配置 backend/.env。" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
hermes_clear_proxy_env

: "${MYSQL_HOST:?MYSQL_HOST is required}"
: "${MYSQL_PORT:?MYSQL_PORT is required}"
: "${MYSQL_USER:?MYSQL_USER is required}"
: "${MYSQL_PASSWORD:?MYSQL_PASSWORD is required}"
: "${MYSQL_DB:?MYSQL_DB is required}"
MYSQL_SSL_MODE="${MYSQL_SSL_MODE:-DISABLED}"

if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install -q -r "$BACKEND_DIR/requirements.txt"

mysql \
  --ssl-mode="$MYSQL_SSL_MODE" \
  -h "$MYSQL_HOST" \
  -P "$MYSQL_PORT" \
  -u "$MYSQL_USER" \
  -p"$MYSQL_PASSWORD" \
  -e "CREATE DATABASE IF NOT EXISTS \`$MYSQL_DB\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

echo "Database bootstrap: creating missing tables from current models"
(
  cd "$BACKEND_DIR"
  "$VENV_DIR/bin/python" -m scripts.schema_bootstrap create
)

echo "Database bootstrap: applying explicit current schema SQL"
mysql \
  --ssl-mode="$MYSQL_SSL_MODE" \
  -h "$MYSQL_HOST" \
  -P "$MYSQL_PORT" \
  -u "$MYSQL_USER" \
  -p"$MYSQL_PASSWORD" \
  "$MYSQL_DB" < "$BACKEND_DIR/scripts/current_schema.sql"

(
  cd "$BACKEND_DIR"
  "$VENV_DIR/bin/python" -m scripts.schema_bootstrap verify
)

(
  cd "$BACKEND_DIR"
  "$VENV_DIR/bin/python" -m scripts.seed
)

echo "Hermes Cloud database initialized: $MYSQL_DB"
