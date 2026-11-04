#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
ENV_FILE="$BACKEND_DIR/.env"
VENV_DIR="$BACKEND_DIR/.venv"

if [[ "${HERMES_FRESH_DB_SMOKE:-0}" != "1" ]]; then
  echo "fresh database smoke: HERMES_FRESH_DB_SMOKE must be 1" >&2
  exit 2
fi

if [[ ! -f "$ENV_FILE" || ! -x "$VENV_DIR/bin/python" ]]; then
  echo "fresh database smoke: backend environment is not initialized" >&2
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

if [[ ! "$MYSQL_DB" =~ ^[A-Za-z0-9_]+$ ]]; then
  echo "fresh database smoke: MYSQL_DB contains unsupported characters" >&2
  exit 2
fi

suffix="$(date +%s)_$(od -An -N4 -tx4 /dev/urandom | tr -d ' ')"
TEMP_DB="${MYSQL_DB}_fresh_smoke_${suffix}"
if [[ "$TEMP_DB" == "$MYSQL_DB" ]]; then
  echo "fresh database smoke: temporary database must differ from MYSQL_DB" >&2
  exit 2
fi

CREATED=0
cleanup() {
  local exit_status=$?
  trap - EXIT
  if [[ "$CREATED" == "1" ]]; then
    if ! mysql \
      --ssl-mode="$MYSQL_SSL_MODE" \
      -h "$MYSQL_HOST" \
      -P "$MYSQL_PORT" \
      -u "$MYSQL_USER" \
      -p"$MYSQL_PASSWORD" \
      -e "DROP DATABASE IF EXISTS \`$TEMP_DB\`;"; then
      echo "fresh database smoke: cleanup failed for temporary database $TEMP_DB" >&2
      if [[ "$exit_status" == "0" ]]; then
        exit_status=1
      fi
    fi
  fi
  exit "$exit_status"
}
trap cleanup EXIT

mysql \
  --ssl-mode="$MYSQL_SSL_MODE" \
  -h "$MYSQL_HOST" \
  -P "$MYSQL_PORT" \
  -u "$MYSQL_USER" \
  -p"$MYSQL_PASSWORD" \
  -e "CREATE DATABASE \`$TEMP_DB\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
CREATED=1

export MYSQL_DB="$TEMP_DB"

(
  cd "$BACKEND_DIR"
  "$VENV_DIR/bin/python" -m scripts.schema_bootstrap create
)

mysql \
  --ssl-mode="$MYSQL_SSL_MODE" \
  -h "$MYSQL_HOST" \
  -P "$MYSQL_PORT" \
  -u "$MYSQL_USER" \
  -p"$MYSQL_PASSWORD" \
  "$TEMP_DB" < "$BACKEND_DIR/scripts/current_schema.sql"

(
  cd "$BACKEND_DIR"
  "$VENV_DIR/bin/python" -m scripts.schema_bootstrap verify
)

echo "fresh database smoke: passed temporary_database=$TEMP_DB"
