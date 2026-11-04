#!/usr/bin/env bash
set -euo pipefail

# Read-only gate for the Runtime storage maintenance window. It intentionally does
# not create directories, patch Kubernetes resources, or stop any process. A
# non-zero exit means the maintenance window must not start.

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

STORAGE_TARGET="${HERMES_RUNTIME_STORAGE_TARGET:-${HERMES_DATA_ROOT:-/data/hermes}}"
STORAGE_SOURCE="${HERMES_RUNTIME_STORAGE_SOURCE:-}"
STORAGE_UUID="${HERMES_RUNTIME_STORAGE_UUID:-}"
STORAGE_FSTYPE="${HERMES_RUNTIME_STORAGE_FSTYPE:-}"
RUNTIME_NAMESPACE="${HERMES_RUNTIME_NAMESPACE:-hermes-runtimes}"

failures=0

ok() {
  printf 'ok: %s\n' "$*"
}

note() {
  printf 'note: %s\n' "$*"
}

block() {
  printf 'blocker: %s\n' "$*" >&2
  failures=$((failures + 1))
}

require_command() {
  local command_name="$1"
  if command -v "$command_name" >/dev/null 2>&1; then
    ok "command available: $command_name"
  else
    block "missing required command: $command_name"
  fi
}

require_kubernetes_permission() {
  local description="$1"
  shift
  local answer
  if ! answer="$(kubectl auth can-i "$@" 2>/dev/null)"; then
    block "unable to check Kubernetes permission: $description"
  elif [[ "$answer" != "yes" ]]; then
    block "missing Kubernetes permission: $description"
  else
    ok "Kubernetes permission: $description"
  fi
}

task_count() {
  local namespace="$1"
  ctr -n "$namespace" tasks ls -q 2>/dev/null | awk 'NF { count += 1 } END { print count + 0 }'
}

printf '%s\n' 'Runtime storage maintenance preflight (read-only)'

if [[ "$(id -u)" -eq 0 ]]; then
  ok 'running as root'
else
  block 'must run as root to inspect containerd and kubelet'
fi

for command_name in awk ctr findmnt jq kubectl realpath rg rsync stat systemctl systemd-escape; do
  require_command "$command_name"
done

if [[ -z "$STORAGE_SOURCE" || -z "$STORAGE_UUID" || -z "$STORAGE_FSTYPE" ]]; then
  block 'HERMES_RUNTIME_STORAGE_SOURCE, HERMES_RUNTIME_STORAGE_UUID and HERMES_RUNTIME_STORAGE_FSTYPE are required'
elif [[ ! -d "$STORAGE_TARGET" ]]; then
  block "Runtime storage target does not exist: $STORAGE_TARGET"
elif [[ ! -r "$STORAGE_TARGET" || ! -w "$STORAGE_TARGET" ]]; then
  block "Runtime storage target is not readable and writable: $STORAGE_TARGET"
else
  actual_source="$(findmnt -n -o SOURCE -T "$STORAGE_TARGET" 2>/dev/null || true)"
  actual_uuid="$(findmnt -n -o UUID -T "$STORAGE_TARGET" 2>/dev/null || true)"
  actual_fstype="$(findmnt -n -o FSTYPE -T "$STORAGE_TARGET" 2>/dev/null || true)"
  if [[ "$actual_source" == "$STORAGE_SOURCE" && "$actual_uuid" == "$STORAGE_UUID" && "$actual_fstype" == "$STORAGE_FSTYPE" ]]; then
    ok "Runtime storage identity matches target: $STORAGE_TARGET"
  else
    block "Runtime storage identity mismatch: expected source=$STORAGE_SOURCE UUID=$STORAGE_UUID FSTYPE=$STORAGE_FSTYPE; actual source=${actual_source:-missing} UUID=${actual_uuid:-missing} FSTYPE=${actual_fstype:-missing}"
  fi
fi

containerd_root="$(awk -F= '/^[[:space:]]*root[[:space:]]*=/ { gsub(/[[:space:]'\''\"]/, "", $2); print $2; exit }' /etc/containerd/config.toml 2>/dev/null || true)"
if [[ "$containerd_root" == "/var/lib/containerd" ]]; then
  ok 'containerd root is /var/lib/containerd'
else
  block "unexpected containerd root: ${containerd_root:-missing}"
fi

if systemctl cat kubelet 2>/dev/null | rg -q -- '--root-dir(?:=|[[:space:]])'; then
      block 'kubelet has a custom --root-dir; add that path to the maintenance scope before proceeding'
else
  ok 'kubelet has no custom --root-dir'
fi

if [[ -d "$STORAGE_TARGET" ]]; then
  ok "Hermes data root exists: $STORAGE_TARGET"
else
  block "Hermes data root is missing: $STORAGE_TARGET"
fi

if ! kubectl get nodes >/dev/null 2>&1; then
  block 'Kubernetes API is not reachable with the current credentials'
else
  ok 'Kubernetes API is reachable'
  require_kubernetes_permission 'list Runtime Pods' list pods -n "$RUNTIME_NAMESPACE"
  require_kubernetes_permission 'list Runtime Revisions' list revisions.serving.knative.dev -n "$RUNTIME_NAMESPACE"
  require_kubernetes_permission 'patch Runtime Revisions' patch revisions.serving.knative.dev -n "$RUNTIME_NAMESPACE"
  require_kubernetes_permission 'list Runtime KPAs' list podautoscalers.autoscaling.internal.knative.dev -n "$RUNTIME_NAMESPACE"
  require_kubernetes_permission 'patch Runtime KPAs' patch podautoscalers.autoscaling.internal.knative.dev -n "$RUNTIME_NAMESPACE"
  require_kubernetes_permission 'delete Runtime KPAs for reviewed re-read' delete podautoscalers.autoscaling.internal.knative.dev -n "$RUNTIME_NAMESPACE"
  require_kubernetes_permission 'get Runtime Deployment scale' get deployments/scale -n "$RUNTIME_NAMESPACE"
  require_kubernetes_permission 'update Runtime Deployment scale' update deployments/scale -n "$RUNTIME_NAMESPACE"
  require_kubernetes_permission 'list Runtime Pods for completion checks' list pods -n "$RUNTIME_NAMESPACE"
  require_kubernetes_permission 'list Knative Serving Deployments' list deployments -n knative-serving
  require_kubernetes_permission 'list Kourier Deployments' list deployments -n kourier-system

  local_pv_count=0
  while IFS=$'\t' read -r pv_name pv_path; do
    [[ -n "$pv_name" ]] || continue
    local_pv_count=$((local_pv_count + 1))
    canonical_path="$(realpath -m -- "$pv_path")"
    if [[ "$canonical_path" == "$STORAGE_TARGET"/* ]]; then
      ok "Local PV path remains under $STORAGE_TARGET: $pv_name"
    else
      block "Local PV path is outside $STORAGE_TARGET: $pv_name -> $pv_path"
    fi
  done < <(kubectl get pv -o json | jq -r '.items[] | select(.spec.local.path != null) | [.metadata.name, .spec.local.path] | @tsv')
  if [[ "$local_pv_count" -gt 0 ]]; then
    ok "Local PV paths inspected: $local_pv_count"
  else
    block 'no Local PV paths were discovered; refuse to assume the migration scope'
  fi
fi

if systemctl is-active --quiet containerd; then
  ok 'containerd is active'
  while IFS= read -r namespace; do
    [[ -n "$namespace" ]] || continue
    note "containerd namespace $namespace has $(task_count "$namespace") task(s); all must be zero before containerd stops"
  done < <(ctr namespaces ls -q)
else
  block 'containerd is not active; cannot inspect the current task inventory'
fi

if command -v crictl >/dev/null 2>&1; then
  ok 'crictl is available for CRI-level task inspection'
else
  note 'crictl is not installed; cutover must use the documented ctr all-namespace task procedure or install and validate crictl before the maintenance window'
fi

if systemctl is-active --quiet docker; then
  moby_tasks="$(task_count moby)"
  note "Docker is active and shares containerd; moby has $moby_tasks task(s), so its containers will stop during the containerd outage"
else
  note 'Docker is inactive'
fi

if [[ "$failures" -gt 0 ]]; then
  printf 'preflight result: BLOCKED (%s blocker(s))\n' "$failures" >&2
  exit 2
fi

printf '%s\n' 'preflight result: PASS (this does not authorize or execute the cutover)'
