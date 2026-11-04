#!/usr/bin/env bash
set -euo pipefail

# Use the image identity already recorded in backend/.env, then restart the
# complete Runtime fleet and the public service mode selected by the caller.
# This entry is intentionally scoped to the 15 test/UAT cluster.

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/no-proxy-env.sh
source "$ROOT_DIR/scripts/lib/no-proxy-env.sh"
# shellcheck source=scripts/lib/project-lifecycle.sh
source "$ROOT_DIR/scripts/lib/project-lifecycle.sh"

usage() {
  echo "Usage: $0 dev|prod" >&2
}

mode="${1:-}"
if [[ "$mode" == "-h" || "$mode" == "--help" ]]; then
  usage
  exit 0
fi
case "$mode" in
  dev|prod) shift ;;
  *) usage; exit 2 ;;
esac

if (($#)); then usage; exit 2; fi

project_lifecycle_acquire_lock
started_ms="$(date +%s%3N)"
last_stage="argument-validation"
receipt_path="$ROOT_DIR/.runtime/project-restart/last.json"
finalize_receipt() {
  local status=$?
  local finished_ms
  finished_ms="$(date +%s%3N)"
  project_lifecycle_write_receipt \
    "$receipt_path" "$mode" "$status" "$last_stage" \
    "$started_ms" "$finished_ms" || true
  if ((status == 0)); then
    echo "restart-project-from-env: elapsed_seconds=$(( (finished_ms - started_ms) / 1000 )) receipt=$receipt_path"
  else
    echo "restart-project-from-env: failed elapsed_seconds=$(( (finished_ms - started_ms) / 1000 )) receipt=$receipt_path" >&2
  fi
  exit "$status"
}
trap finalize_receipt EXIT

ENV_FILE="$ROOT_DIR/backend/.env"
last_stage="environment-validation"
[[ -f "$ENV_FILE" ]] || {
  echo "restart-project-from-env: missing $ENV_FILE" >&2
  exit 1
}
git -C "$ROOT_DIR" check-ignore -q "$ENV_FILE" || {
  echo "restart-project-from-env: backend/.env must remain git-ignored" >&2
  exit 1
}

echo "restart-project-from-env: mode=$mode image sourced from backend/.env"
"$ROOT_DIR/backend/.venv/bin/python" "$ROOT_DIR/backend/scripts/runtime_image_preflight.py" \
  --env-file "$ENV_FILE" >/dev/null

# Build current frontend sources for every prod restart. Dev mode loads source
# directly through Vite and therefore does not need a static build.
if [[ "$mode" == prod ]]; then
  last_stage="frontend-build"
  echo "restart-project-from-env: building latest User/Admin static bundles"
  bash "$ROOT_DIR/scripts/build-public-prod.sh" 6>&-
fi

# Always hand Nginx back to the published static configuration before
# stopping Vite. The switch uses repository templates declared in .env.
last_stage="nginx-handoff-prod"
bash "$ROOT_DIR/scripts/switch-public-nginx-mode.sh" prod 6>&-
bash "$ROOT_DIR/scripts/stop-public-frontends-dev.sh" 6>&-
if [[ "$mode" == prod ]]; then
  last_stage="prod-lifecycle-preflight"
  bash "$ROOT_DIR/scripts/status-public-prod.sh" 6>&- >/dev/null
fi

last_stage="runtime-reload"
echo "restart-project-from-env: reloading all Runtime Bindings"
bash "$ROOT_DIR/scripts/reload-all-runtimes-from-env-dev.sh" 6>&- \
  --force \
  --concurrency 4 \
  --stop-concurrency 8 \
  --backend-lifecycle "$mode" \
  --reexecute-completed

if [[ "$mode" == dev ]]; then
  last_stage="dev-frontend-start"
  echo "restart-project-from-env: starting dev Backend frontend overlay"
  PUBLIC_FRONTENDS_BACKEND_MODE=dev \
    bash "$ROOT_DIR/scripts/start-public-frontends-dev.sh" 6>&-
  last_stage="nginx-handoff-dev"
  bash "$ROOT_DIR/scripts/switch-public-nginx-mode.sh" dev 6>&-
  [[ "$(bash "$ROOT_DIR/scripts/public-nginx-mode.sh" 6>&- 2>/dev/null)" == dev ]] \
    || { echo "restart-project-from-env: Nginx 未进入 dev 模式" >&2; exit 1; }
else
  last_stage="prod-final-verification"
  [[ "$(bash "$ROOT_DIR/scripts/public-nginx-mode.sh" 6>&- 2>/dev/null)" == prod ]] \
    || { echo "restart-project-from-env: Nginx 未保持 prod 模式" >&2; exit 1; }
  echo "restart-project-from-env: prod Backend and static Nginx are ready"
fi

last_stage="completed"
echo "restart-project-from-env: complete mode=$mode"
