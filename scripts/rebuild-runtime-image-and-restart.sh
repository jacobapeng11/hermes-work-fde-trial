#!/usr/bin/env bash
set -euo pipefail

# Build exactly one immutable Runtime code image from the current workspace,
# atomically update backend/.env through the existing publisher, then run the
# complete project restart using that new image.

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
receipt_path="$ROOT_DIR/.runtime/project-restart/last-rebuild.json"
finalize_receipt() {
  local status=$?
  local finished_ms
  finished_ms="$(date +%s%3N)"
  project_lifecycle_write_receipt \
    "$receipt_path" "$mode" "$status" "$last_stage" \
    "$started_ms" "$finished_ms" || true
  if ((status == 0)); then
    echo "rebuild-runtime-image-and-restart: elapsed_seconds=$(( (finished_ms - started_ms) / 1000 )) receipt=$receipt_path"
  else
    echo "rebuild-runtime-image-and-restart: failed elapsed_seconds=$(( (finished_ms - started_ms) / 1000 )) receipt=$receipt_path" >&2
  fi
  exit "$status"
}
trap finalize_receipt EXIT

ENV_FILE="$ROOT_DIR/backend/.env"
last_stage="environment-validation"
[[ -f "$ENV_FILE" ]] || {
  echo "rebuild-runtime-image-and-restart: missing $ENV_FILE" >&2
  exit 1
}
git -C "$ROOT_DIR" check-ignore -q "$ENV_FILE" || {
  echo "rebuild-runtime-image-and-restart: backend/.env must remain git-ignored" >&2
  exit 1
}

echo "rebuild-runtime-image-and-restart: building one Runtime code image"
last_stage="runtime-image-build"
bash "$ROOT_DIR/scripts/build-runtime-code-image-dev.sh" --env-file "$ENV_FILE" 6>&-

echo "rebuild-runtime-image-and-restart: restarting project with the new .env image"
last_stage="project-restart"
HERMES_PROJECT_LOCK_HELD=1 \
  bash "$ROOT_DIR/scripts/restart-project-from-env.sh" "$mode" 6>&-

last_stage="completed"
echo "rebuild-runtime-image-and-restart: complete mode=$mode"
