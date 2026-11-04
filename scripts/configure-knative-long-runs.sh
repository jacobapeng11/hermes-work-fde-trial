#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

TIMEOUT_SECONDS="${HERMES_RUNTIME_LONG_REQUEST_TIMEOUT_SECONDS:-3900}"
KNATIVE_NAMESPACE="${KNATIVE_SERVING_NAMESPACE:-knative-serving}"

if ! [[ "$TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] || (( TIMEOUT_SECONDS < 1 )); then
  echo "HERMES_RUNTIME_LONG_REQUEST_TIMEOUT_SECONDS must be a positive integer" >&2
  exit 2
fi

kubectl -n "$KNATIVE_NAMESPACE" patch configmap config-defaults --type merge \
  -p "{\"data\":{\"max-revision-timeout-seconds\":\"$TIMEOUT_SECONDS\"}}"

# The Runtime manifest depends on writable PVC/emptyDir volumes and an
# explicit PodSecurityContext for the shared fsGroup.  Knative validates these
# fields behind feature flags; configure all required gates together so a
# manifest cannot pass long-run setup while silently losing its security or
# durability contract.
kubectl -n "$KNATIVE_NAMESPACE" patch configmap config-features --type merge \
  -p '{"data":{"kubernetes.podspec-persistent-volume-claim":"enabled","kubernetes.podspec-persistent-volume-write":"enabled","kubernetes.podspec-securitycontext":"enabled","kubernetes.podspec-volumes-emptydir":"enabled","kubernetes.podspec-fieldref":"enabled","kubernetes.containerspec-addcapabilities":"enabled"}}'

# Knative recommends keeping the activator alive for at least the longest
# admitted request so a rollout does not cut an in-flight Dashboard stream.
kubectl -n "$KNATIVE_NAMESPACE" patch deployment activator --type merge \
  -p "{\"spec\":{\"template\":{\"spec\":{\"terminationGracePeriodSeconds\":$TIMEOUT_SECONDS}}}}"

echo "Knative Runtime features and long-run ceiling configured: ${TIMEOUT_SECONDS}s"
