#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$ROOT_DIR/deploy/kubernetes/admin-resource-reader-rbac.yaml"
OUTPUT="${ADMIN_KUBECONFIG_OUTPUT:-$ROOT_DIR/.runtime/public-dev/admin-kubernetes.kubeconfig}"
NAMESPACE="${HERMES_RUNTIME_NAMESPACE:-hermes-runtimes}"
SERVICE_ACCOUNT="hermes-admin-resource-reader"
TOKEN_SECRET="hermes-admin-resource-reader-token"

command -v kubectl >/dev/null 2>&1 || {
  echo "install-admin-kubernetes-access: kubectl is required" >&2
  exit 2
}

kubectl apply -f "$MANIFEST"

token=""
ca_data=""
for _ in $(seq 1 30); do
  token="$(kubectl -n "$NAMESPACE" get secret "$TOKEN_SECRET" -o jsonpath='{.data.token}' 2>/dev/null || true)"
  ca_data="$(kubectl -n "$NAMESPACE" get secret "$TOKEN_SECRET" -o jsonpath='{.data.ca\.crt}' 2>/dev/null || true)"
  if [[ -n "$token" && -n "$ca_data" ]]; then
    break
  fi
  sleep 1
done

if [[ -z "$token" || -z "$ca_data" ]]; then
  echo "install-admin-kubernetes-access: service account token was not populated" >&2
  exit 2
fi

server="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
if [[ -z "$server" ]]; then
  echo "install-admin-kubernetes-access: current Kubernetes server is unavailable" >&2
  exit 2
fi

mkdir -p "$(dirname "$OUTPUT")"
temp_dir="$(mktemp -d)"
trap 'rm -rf "$temp_dir"' EXIT
ca_file="$temp_dir/ca.crt"
temp_kubeconfig="$temp_dir/kubeconfig"
printf '%s' "$ca_data" | base64 -d >"$ca_file"

kubectl config --kubeconfig "$temp_kubeconfig" set-cluster hermes-admin \
  --server="$server" \
  --certificate-authority="$ca_file" \
  --embed-certs=true >/dev/null
kubectl config --kubeconfig "$temp_kubeconfig" set-credentials "$SERVICE_ACCOUNT" \
  --token="$(printf '%s' "$token" | base64 -d)" >/dev/null
kubectl config --kubeconfig "$temp_kubeconfig" set-context hermes-admin \
  --cluster=hermes-admin \
  --user="$SERVICE_ACCOUNT" \
  --namespace="$NAMESPACE" >/dev/null
kubectl config --kubeconfig "$temp_kubeconfig" use-context hermes-admin >/dev/null

umask 077
install -m 600 "$temp_kubeconfig" "$OUTPUT"
KUBECONFIG="$OUTPUT" bash "$ROOT_DIR/scripts/check-admin-kubernetes-rbac.sh"
echo "install-admin-kubernetes-access: ready at $OUTPUT"
