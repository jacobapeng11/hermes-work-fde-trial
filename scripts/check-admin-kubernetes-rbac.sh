#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

RUNTIME_NAMESPACE="${HERMES_RUNTIME_NAMESPACE:-hermes-runtimes}"

can() {
  kubectl auth can-i "$@"
}

require_yes() {
  local description="$1"
  shift
  if [[ "$(can "$@")" != "yes" ]]; then
    echo "admin kubernetes RBAC gate: missing $description" >&2
    exit 3
  fi
}

require_no() {
  local description="$1"
  shift
  if [[ "$(can "$@")" != "no" ]]; then
    echo "admin kubernetes RBAC gate: forbidden permission is open: $description" >&2
    exit 4
  fi
}

require_yes "get nodes" get nodes
require_yes "list namespaces" list namespaces
require_yes "list runtime pods" list pods -n "$RUNTIME_NAMESPACE"
require_yes "get pods/log" get pods --subresource=log -n "$RUNTIME_NAMESPACE"
require_yes "delete pods" delete pods -n "$RUNTIME_NAMESPACE"
require_yes "list Knative services" list services.serving.knative.dev -n "$RUNTIME_NAMESPACE"
require_yes "patch revisions.serving.knative.dev" patch revisions.serving.knative.dev -n "$RUNTIME_NAMESPACE"
require_yes "patch podautoscalers.autoscaling.internal.knative.dev" patch podautoscalers.autoscaling.internal.knative.dev -n "$RUNTIME_NAMESPACE"
require_yes "read knative-serving deployments" list deployments.apps -n knative-serving
require_yes "read kourier-system deployments" list deployments.apps -n kourier-system

require_no "pods/exec" create pods --subresource=exec -n "$RUNTIME_NAMESPACE"
require_no "secrets" get secrets -n "$RUNTIME_NAMESPACE"
require_no "PVC deletion" delete persistentvolumeclaims -n "$RUNTIME_NAMESPACE"
require_no "runtime Service reads" list services -n "$RUNTIME_NAMESPACE"
require_no "EndpointSlice reads" list endpointslices.discovery.k8s.io -n "$RUNTIME_NAMESPACE"
require_no "Service deletion" delete services -n "$RUNTIME_NAMESPACE"
require_no "EndpointSlice mutation" patch endpointslices.discovery.k8s.io -n "$RUNTIME_NAMESPACE"
require_no "Deployment patch" patch deployments.apps -n "$RUNTIME_NAMESPACE"
require_no "Knative control-plane patch" patch deployments.apps -n knative-serving

echo "admin kubernetes RBAC gate: ok"
