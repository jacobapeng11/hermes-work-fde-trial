#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a
# shellcheck disable=SC1091
source "$ROOT_DIR/backend/.env"
set +a
IMAGE="${HERMES_RUNTIME_IMAGE:?backend/.env 缺少 HERMES_RUNTIME_IMAGE}"
ARTIFACTS_DIR=""

usage() {
  echo "Usage: scripts/test-hermes-browser-runtime-container.sh --artifacts <empty-directory> [--image <image>]"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --artifacts) ARTIFACTS_DIR="${2:-}"; shift 2 ;;
    --image) IMAGE="${2:-}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

[[ -n "$ARTIFACTS_DIR" ]] || { usage >&2; exit 2; }
docker image inspect "$IMAGE" >/dev/null
if [[ -e "$ARTIFACTS_DIR" ]]; then
  [[ -d "$ARTIFACTS_DIR" && -z "$(find "$ARTIFACTS_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]] || { echo "artifacts directory must exist and be empty" >&2; exit 2; }
else
  mkdir -p "$ARTIFACTS_DIR"
fi
ARTIFACTS_DIR="$(cd "$ARTIFACTS_DIR" && pwd -P)"
mkdir -p "$ARTIFACTS_DIR/logs"
LOG_PATH="$ARTIFACTS_DIR/logs/container-test.log"

set +e
docker run --rm --network none --read-only --tmpfs /tmp:exec,mode=1777 \
  --mount "type=bind,src=$ARTIFACTS_DIR,dst=/artifacts" \
  --entrypoint /usr/local/bin/hermes-browser-container-test "$IMAGE" /artifacts 2>&1 | tee "$LOG_PATH"
status=${PIPESTATUS[0]}
set -e

if rg -n '(^|[^[:alpha:]])(npm install|npx|agent-browser install|apt-get|apk add)([^[:alpha:]]|$)' "$LOG_PATH"; then
  echo "runtime installation command detected in container output" >&2
  exit 1
fi
[[ "$status" -eq 0 ]] || { echo "Browser Runtime container acceptance failed; inspect $LOG_PATH" >&2; exit "$status"; }
printf 'Browser Runtime container acceptance: PASS (%s)\nArtifacts: %s\n' "$IMAGE" "$ARTIFACTS_DIR"
