#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

VERSION="v0.8.1"
MANIFEST_URL="https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.8.1/components.yaml"
MANIFEST_SHA256="4a672c4891902573a3ff753cece5de1bf1f55dd053403dfec39df9d1636b7ff1"
INSECURE_TLS="${METRICS_SERVER_KUBELET_INSECURE_TLS:-0}"
WORK_DIR="$(mktemp -d)"
MANIFEST_PATH="${WORK_DIR}/components.yaml"
trap 'rm -rf "$WORK_DIR"' EXIT

case "$INSECURE_TLS" in
  0|1) ;;
  *)
    echo "METRICS_SERVER_KUBELET_INSECURE_TLS must be 0 or 1" >&2
    exit 2
    ;;
esac

for command in curl sha256sum kubectl awk; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "required command not found: $command" >&2
    exit 2
  }
done

curl -fsSL "$MANIFEST_URL" -o "$MANIFEST_PATH"
echo "${MANIFEST_SHA256}  ${MANIFEST_PATH}" | sha256sum --check --status || {
  echo "metrics-server manifest checksum verification failed" >&2
  exit 1
}

if [[ "$INSECURE_TLS" == "1" ]]; then
  PATCHED_PATH="${WORK_DIR}/components-insecure-kubelet-tls.yaml"
  awk '
    { print }
    /- --metric-resolution=15s/ {
      print "        - --kubelet-insecure-tls"
    }
  ' "$MANIFEST_PATH" > "$PATCHED_PATH"
  MANIFEST_PATH="$PATCHED_PATH"
fi

kubectl apply -f "$MANIFEST_PATH"
kubectl -n kube-system rollout status deployment/metrics-server --timeout=180s
kubectl wait --for=condition=Available apiservice/v1beta1.metrics.k8s.io --timeout=180s

retry() {
  local attempts="$1"
  shift
  local attempt
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if "$@"; then
      return 0
    fi
    sleep 5
  done
  return 1
}

retry 24 kubectl top nodes
retry 24 kubectl top pods -A

verify_hpa() {
  local namespace="$1"
  local name="$2"
  local hpa_status
  hpa_status="$(
    kubectl -n "$namespace" get hpa "$name" \
      -o jsonpath='{range .status.conditions[?(@.type=="ScalingActive")]}{.status}{end}'
  )"
  if [[ "$hpa_status" != "True" ]]; then
    echo "HPA ${namespace}/${name} is not ScalingActive" >&2
    kubectl -n "$namespace" describe hpa "$name" >&2
    return 1
  fi
}

retry 24 verify_hpa knative-serving activator
retry 24 verify_hpa knative-serving webhook
retry 24 verify_hpa kourier-system 3scale-kourier-gateway

echo "metrics-server ${VERSION} is ready; Metrics API, kubectl top, and system HPAs are healthy"
