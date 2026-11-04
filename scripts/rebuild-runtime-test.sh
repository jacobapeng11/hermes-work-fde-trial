#!/usr/bin/env bash
set -euo pipefail

# Test-environment fast path: build one Supervisor overlay and force-refresh
# every available Runtime. Backend reloads the new image; no frontend build.
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/no-proxy-env.sh"
source "$ROOT_DIR/scripts/lib/project-lifecycle.sh"

case "${1:-}" in
  --help|-h)
    echo "Usage: bash scripts/rebuild-runtime-test.sh [--print-plan|--resume]"
    echo "Build Supervisor once, then force-switch all Runtime Bindings; preserve the Backend mode."
    echo "--resume retries the current image cutover without rebuilding."
    exit 0 ;;
  ""|--print-plan|--resume) ;;
  *) echo "Unknown option: $1" >&2; exit 2 ;;
esac
if (($# > 1)); then echo "Expected at most one option" >&2; exit 2; fi

ENV_FILE="$ROOT_DIR/backend/.env"
if [[ "${1:-}" == --print-plan ]]; then
  bash "$ROOT_DIR/scripts/build-runtime-code-image-dev.sh" --env-file "$ENV_FILE" --print-plan
  echo "Next: force-switch all available personal/application Runtimes; restart current Backend; no frontend build."
  exit 0
fi
project_lifecycle_acquire_lock
started_seconds=$SECONDS
if [[ "${1:-}" != --resume ]]; then
  bash "$ROOT_DIR/scripts/build-runtime-code-image-dev.sh" --env-file "$ENV_FILE" 6>&-
fi
bash "$ROOT_DIR/scripts/reload-all-runtimes-from-env-dev.sh" \
  --force \
  --concurrency 4 \
  --stop-concurrency 8 \
  --backend-lifecycle auto \
  --skip-unavailable \
  --supersede-failed-target \
  --reexecute-completed 6>&-
echo "rebuild-runtime-test: completed elapsed_seconds=$((SECONDS - started_seconds))"
