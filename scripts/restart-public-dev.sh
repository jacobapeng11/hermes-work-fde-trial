#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib/public-dev-lifecycle.sh"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib/public-mode-handoff.sh"
public_mode_acquire_lock
public_dev_acquire_lock
export PUBLIC_DEV_LOCK_HELD=1
public_dev_load_backend_env
public_dev_resolve_ports

fail() {
  echo "restart-public-dev: $*" >&2
  exit 1
}

echo "restart-public-dev: stopping local stack and scaling project Runtimes to zero"
if ! bash "$ROOT_DIR/scripts/stop-public-dev.sh"; then
  fail "complete stop failed; project remains stopped and restart was not attempted."
fi

echo "restart-public-dev: starting local stack without proactive Runtime warmup"
INIT_DB=0 WARMUP_RUNTIME=0 bash "$ROOT_DIR/scripts/start-public-dev.sh"

echo "restart-public-dev: complete"
