#!/usr/bin/env bash
set -euo pipefail

# Controlled Runtime zero for the storage-maintenance window. `plan` is
# read-only. `zero` and `restore` intentionally require an explicit state file
# under the configured Runtime storage target, so temporary autoscaling
# annotations are recoverable after a reboot.

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${HERMES_RUNTIME_NAMESPACE:-hermes-runtimes}"
STORAGE_TARGET="${HERMES_RUNTIME_STORAGE_TARGET:-${HERMES_DATA_ROOT:-/data/hermes}}"
PROJECT_SELECTOR="serving.knative.dev/service,hermes.platform/runtime-id"
CONTROL_SCRIPT="$ROOT_DIR/scripts/lib/public_dev_runtime_control.py"
STATE_FILE=""
COMMAND="${1:-}"

usage() {
  cat <<'EOF'
Usage:
  bash scripts/runtime-vdb-maintenance-zero.sh plan
  bash scripts/runtime-vdb-maintenance-zero.sh zero --state-file /data/hermes/migration-state/autoscaling.json
  bash scripts/runtime-vdb-maintenance-zero.sh restore --state-file /data/hermes/migration-state/autoscaling.json

`zero` mutates Knative resources only after writing the annotation snapshot.
`restore` re-applies that snapshot. Both commands are for an approved
maintenance window; `plan` is the only read-only command.
EOF
}

require_state_file() {
  [[ -n "$STATE_FILE" ]] || {
    echo 'runtime-vdb maintenance zero: --state-file is required' >&2
    exit 2
  }
  canonical_state="$(realpath -m -- "$STATE_FILE")"
  canonical_mount="$(realpath -m -- "$STORAGE_TARGET")"
  case "$canonical_state" in
    "$canonical_mount"/*) STATE_FILE="$canonical_state" ;;
    *)
      echo "runtime-vdb maintenance zero: state file must be under $canonical_mount" >&2
      exit 2
      ;;
  esac
}

parse_state_file() {
  shift
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --state-file)
        [[ "$#" -ge 2 ]] || {
          echo 'runtime-vdb maintenance zero: --state-file needs a path' >&2
          exit 2
        }
        STATE_FILE="$2"
        shift 2
        ;;
      *)
        echo "runtime-vdb maintenance zero: unexpected argument: $1" >&2
        exit 2
        ;;
    esac
  done
}

snapshot_resource() {
  local resource="$1"
  kubectl -n "$NAMESPACE" get "$resource" -l "$PROJECT_SELECTOR" -o json \
    | jq --arg resource "$resource" '
      [.items[] | {
        resource: $resource,
        name: .metadata.name,
        annotations: {
          "autoscaling.knative.dev/scale-down-delay": (.metadata.annotations["autoscaling.knative.dev/scale-down-delay"] // null),
          "autoscaling.knative.dev/window": (.metadata.annotations["autoscaling.knative.dev/window"] // null),
          "autoscaling.knative.dev/scale-to-zero-pod-retention-period": (.metadata.annotations["autoscaling.knative.dev/scale-to-zero-pod-retention-period"] // null)
        }
      }]'
}

write_snapshot() {
  [[ ! -e "$STATE_FILE" ]] || {
    echo "runtime-vdb maintenance zero: refusing to overwrite state file: $STATE_FILE" >&2
    exit 2
  }
  local state_dir temporary revisions kpas
  state_dir="$(dirname "$STATE_FILE")"
  install -d -m 0700 "$state_dir"
  revisions="$(snapshot_resource revisions.serving.knative.dev)"
  kpas="$(snapshot_resource podautoscalers.autoscaling.internal.knative.dev)"
  temporary="${STATE_FILE}.tmp.$$"
  umask 077
  jq -n \
    --arg created_at "$(date --iso-8601=seconds)" \
    --arg namespace "$NAMESPACE" \
    --argjson revisions "$revisions" \
    --argjson kpas "$kpas" \
    '{version: 1, created_at: $created_at, namespace: $namespace, records: ($revisions + $kpas)}' \
    >"$temporary"
  mv -- "$temporary" "$STATE_FILE"
  printf 'runtime-vdb maintenance zero: annotation snapshot written: %s\n' "$STATE_FILE"
}

require_deployments_ready() {
  local namespace="$1"
  local unhealthy=0
  local name desired available updated observed generation
  while IFS=$'\t' read -r name desired available updated observed generation; do
    [[ -n "$name" ]] || continue
    if (( available < desired || updated < desired || observed < generation )); then
      echo "runtime-vdb maintenance zero: control-plane Deployment is not ready: $namespace/$name desired=$desired available=$available updated=$updated observed=$observed generation=$generation" >&2
      unhealthy=1
    fi
  done < <(
    kubectl -n "$namespace" get deployments -o json | jq -r '
      .items[] |
      [
        .metadata.name,
        (.spec.replicas // 1),
        (.status.availableReplicas // 0),
        (.status.updatedReplicas // 0),
        (.status.observedGeneration // 0),
        .metadata.generation
      ] | @tsv'
  )
  [[ "$unhealthy" -eq 0 ]] || return 1
}

plan() {
  printf 'Runtime Revisions in namespace %s:\n' "$NAMESPACE"
  kubectl -n "$NAMESPACE" get revisions.serving.knative.dev -l "$PROJECT_SELECTOR" \
    -o custom-columns=NAME:.metadata.name,DELAY:.metadata.annotations.autoscaling\\.knative\\.dev/scale-down-delay --no-headers
  printf 'Runtime KPAs in namespace %s:\n' "$NAMESPACE"
  kubectl -n "$NAMESPACE" get podautoscalers.autoscaling.internal.knative.dev -l "$PROJECT_SELECTOR" \
    -o custom-columns=NAME:.metadata.name,DESIRED:.status.desiredScale,ACTUAL:.status.actualScale --no-headers
}

zero() {
  require_state_file
  [[ -d "$STORAGE_TARGET" && -r "$STORAGE_TARGET" && -w "$STORAGE_TARGET" ]] || {
    echo "runtime-vdb maintenance zero: Runtime storage target must be readable and writable: $STORAGE_TARGET" >&2
    exit 2
  }
  require_deployments_ready knative-serving
  require_deployments_ready kourier-system
  write_snapshot
  python3 "$CONTROL_SCRIPT" force-zero \
    --namespace "$NAMESPACE" \
    --include-application-agents \
    --keep-annotations
  printf 'runtime-vdb maintenance zero: complete; annotations remain in maintenance mode until restore\n'
}

restore() {
  require_state_file
  [[ -f "$STATE_FILE" ]] || {
    echo "runtime-vdb maintenance zero: state file does not exist: $STATE_FILE" >&2
    exit 2
  }
  state_namespace="$(jq -r '.namespace // empty' "$STATE_FILE")"
  [[ "$state_namespace" == "$NAMESPACE" ]] || {
    echo "runtime-vdb maintenance zero: state namespace mismatch: $state_namespace" >&2
    exit 2
  }
  while IFS= read -r record; do
    resource="$(jq -r '.resource' <<<"$record")"
    name="$(jq -r '.name' <<<"$record")"
    annotations="$(jq -c '.annotations' <<<"$record")"
    patch="$(jq -cn --argjson annotations "$annotations" '{metadata: {annotations: $annotations}}')"
    kubectl -n "$NAMESPACE" patch "$resource" "$name" --type=merge -p "$patch"
  done < <(jq -c '.records[]' "$STATE_FILE")
  printf 'runtime-vdb maintenance zero: annotations restored from %s\n' "$STATE_FILE"
}

case "$COMMAND" in
  plan)
    [[ "$#" -eq 1 ]] || { usage >&2; exit 2; }
    plan
    ;;
  zero)
    parse_state_file "$@"
    zero
    ;;
  restore)
    parse_state_file "$@"
    restore
    ;;
  -h|--help|help|'')
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
