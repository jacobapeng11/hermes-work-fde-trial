#!/usr/bin/env bash
set -euo pipefail

# Read-only Runtime storage identity gate. The target may be an ordinary
# directory on the root filesystem; only its actual filesystem identity and
# read/write capability are part of this contract.

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"
# shellcheck source=scripts/lib/private-env-file.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/private-env-file.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "${1:-hermes-data}" in
  hermes-data|all) ;;
  *)
    echo "usage: $0 [hermes-data]" >&2
    exit 2
    ;;
esac

# Storage identity is an environment contract, not a property of the code
# checkout. Values may be exported by the lifecycle or loaded from the
# selected private environment file.
if [[ -z "${HERMES_RUNTIME_STORAGE_SOURCE:-}"
  || -z "${HERMES_RUNTIME_STORAGE_UUID:-}"
  || -z "${HERMES_RUNTIME_STORAGE_FSTYPE:-}" ]]; then
  storage_env_file="${HERMES_STORAGE_ENV_FILE:-$ROOT_DIR/backend/.env}"
  if [[ -n "$storage_env_file" && -f "$storage_env_file" ]]; then
    hermes_require_private_env_file "$storage_env_file"
    set -a
    # shellcheck disable=SC1090
    source "$storage_env_file"
    set +a
  fi
fi

expected_source="${HERMES_RUNTIME_STORAGE_SOURCE:-}"
expected_uuid="${HERMES_RUNTIME_STORAGE_UUID:-}"
expected_fstype="${HERMES_RUNTIME_STORAGE_FSTYPE:-}"
target_path="${HERMES_RUNTIME_STORAGE_TARGET:-${HERMES_DATA_ROOT:-/data/hermes}}"

if [[ -z "$expected_source" || -z "$expected_uuid" || -z "$expected_fstype" ]]; then
  echo "runtime storage guard: HERMES_RUNTIME_STORAGE_SOURCE, HERMES_RUNTIME_STORAGE_UUID and HERMES_RUNTIME_STORAGE_FSTYPE are required" >&2
  exit 2
fi

if [[ ! -d "$target_path" ]]; then
  echo "runtime storage guard: target directory does not exist: $target_path" >&2
  exit 1
fi
target_mode="$(stat -Lc '%a' -- "$target_path" 2>/dev/null || true)"
if [[ ! -r "$target_path" || ! -w "$target_path" || -z "$target_mode" ]]; then
  echo "runtime storage guard: target directory is not readable and writable: $target_path" >&2
  exit 1
fi
target_permissions=$((8#$target_mode))
if (( (target_permissions & 0444) == 0 || (target_permissions & 0222) == 0 )); then
  echo "runtime storage guard: target directory is not readable and writable: $target_path" >&2
  exit 1
fi

actual_source=""
actual_uuid=""
actual_fstype=""
if ! actual_source="$(findmnt -n -o SOURCE -T "$target_path" 2>/dev/null)"; then
  echo "runtime storage guard: findmnt could not resolve target filesystem: $target_path" >&2
  exit 1
fi
if ! actual_uuid="$(findmnt -n -o UUID -T "$target_path" 2>/dev/null)"; then
  echo "runtime storage guard: findmnt could not resolve target UUID: $target_path" >&2
  exit 1
fi
if ! actual_fstype="$(findmnt -n -o FSTYPE -T "$target_path" 2>/dev/null)"; then
  echo "runtime storage guard: findmnt could not resolve target filesystem type: $target_path" >&2
  exit 1
fi

if [[ "$actual_source" != "$expected_source"
  || "$actual_uuid" != "$expected_uuid"
  || "$actual_fstype" != "$expected_fstype" ]]; then
  echo "runtime storage guard: filesystem identity mismatch for $target_path" >&2
  echo "  expected source=$expected_source UUID=$expected_uuid FSTYPE=$expected_fstype" >&2
  echo "  actual   source=${actual_source:-missing} UUID=${actual_uuid:-missing} FSTYPE=${actual_fstype:-missing}" >&2
  exit 1
fi

echo "runtime storage guard: PASS target=$target_path source=$actual_source UUID=$actual_uuid FSTYPE=$actual_fstype"
