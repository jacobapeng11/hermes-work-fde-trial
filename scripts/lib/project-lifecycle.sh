#!/usr/bin/env bash

# Lock and receipt helpers for the complete project restart entrypoints.
# Callers must set ROOT_DIR before sourcing this file.

project_lifecycle_acquire_lock() {
  if [[ "${HERMES_PROJECT_LOCK_HELD:-0}" == "1" ]]; then
    return 0
  fi
  command -v flock >/dev/null 2>&1 || {
    echo "project lifecycle: flock is required" >&2
    return 1
  }
  mkdir -p "$ROOT_DIR/.runtime/project-restart"
  exec 6>"$ROOT_DIR/.runtime/project-restart/lifecycle.lock"
  if ! flock -w "${PROJECT_LIFECYCLE_LOCK_TIMEOUT_SECONDS:-30}" 6; then
    echo "project lifecycle: timed out waiting for project restart lock" >&2
    return 1
  fi
  HERMES_PROJECT_LOCK_HELD=1
}

project_lifecycle_write_receipt() {
  local path="$1"
  local mode="$2"
  local status_code="$3"
  local last_stage="$4"
  local started_ms="$5"
  local finished_ms="$6"
  mkdir -p "$(dirname "$path")"
  python3 - "$path" "$mode" "$status_code" "$last_stage" "$started_ms" "$finished_ms" <<'PY'
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

path, mode, status_code, last_stage, started_ms, finished_ms = sys.argv[1:]
started = int(started_ms)
finished = int(finished_ms)
document = {
    "contract_version": 1,
    "mode": mode,
    "status": "completed" if int(status_code) == 0 else "failed",
    "exit_code": int(status_code),
    "last_stage": last_stage,
    "started_at": datetime.fromtimestamp(started / 1000, timezone.utc).isoformat(),
    "finished_at": datetime.fromtimestamp(finished / 1000, timezone.utc).isoformat(),
    "elapsed_seconds": round(max(0, finished - started) / 1000, 3),
}
target = Path(path)
temporary = target.with_name(f".{target.name}.tmp")
temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
temporary.chmod(0o600)
temporary.replace(target)
PY
}
