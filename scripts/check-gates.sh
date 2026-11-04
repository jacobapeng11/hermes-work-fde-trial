#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/backend/.env"

# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib/public-dev-lifecycle.sh"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Gate 类型: environment"
  echo "阻塞项: backend/.env 不存在。"
  echo "建议: 直接创建并配置 backend/.env。"
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
hermes_clear_proxy_env

missing=0

mark_missing() {
  local gate="$1"
  local field="$2"
  echo "[$gate] missing: $field"
  missing=1
}

mark_placeholder() {
  local gate="$1"
  local field="$2"
  echo "[$gate] placeholder: $field"
  missing=1
}

require_nonempty() {
  local gate="$1"
  local field="$2"
  local value="${!field:-}"
  if [[ -z "$value" ]]; then
    mark_missing "$gate" "$field"
  fi
}

require_not_value() {
  local gate="$1"
  local field="$2"
  local forbidden="$3"
  local value="${!field:-}"
  if [[ "$value" == "$forbidden" ]]; then
    mark_placeholder "$gate" "$field"
  fi
}

echo "Checking Hermes Cloud gate configuration"

require_nonempty "database" "MYSQL_HOST"
require_nonempty "database" "MYSQL_PORT"
require_nonempty "database" "MYSQL_USER"
require_nonempty "database" "MYSQL_PASSWORD"
require_nonempty "database" "MYSQL_DB"
require_not_value "database" "MYSQL_PASSWORD" "change-me"

require_nonempty "k8s-knative" "HERMES_RUNTIME_NAMESPACE"
require_nonempty "k8s-knative" "HERMES_RUNTIME_SERVICE_ACCOUNT"
require_not_value "k8s-knative" "HERMES_RUNTIME_SERVICE_ACCOUNT" "hermes-runtime"
require_nonempty "k8s-knative" "HERMES_RUNTIME_IMAGE"
require_nonempty "k8s-knative" "HERMES_RUNTIME_INGRESS_BASE_URL"
require_nonempty "k8s-knative" "HERMES_RUNTIME_ROOT"
require_nonempty "k8s-knative" "HERMES_RUNTIME_NODE_NAME"
require_nonempty "k8s-knative" "ENTERPRISE_KNOWLEDGE_HOST_PATH"
require_nonempty "k8s-knative" "PUBLIC_API_BASE"
require_not_value "k8s-knative" "HERMES_RUNTIME_IMAGE" "hermes-agent:cloud"

require_nonempty "enterprise-data" "KNOWLEDGE_DATA_SERVICE_BASE_URL"
require_nonempty "enterprise-data" "DATA_SERVICE_ASSERTION_SECRET"
require_nonempty "enterprise-data" "DATA_SERVICE_ASSERTION_AUDIENCE"

if ! public_dev_require_enterprise_knowledge_bundle; then
  missing=1
fi

if [[ "${PUBLIC_API_BASE:-}" == http://127.0.0.1:* || "${PUBLIC_API_BASE:-}" == http://localhost:* ]]; then
  echo "[k8s-knative] placeholder: PUBLIC_API_BASE must be reachable from Hermes runtime pods"
  missing=1
fi

if [[ -z "${KUBECONFIG:-}" && -z "${KUBERNETES_SERVICE_HOST:-}" ]]; then
  echo "[k8s-knative] missing: KUBECONFIG or in-cluster Kubernetes service environment"
  missing=1
fi

if [[ -z "${ADMIN_KUBECONFIG:-}" ]]; then
  default_admin_kubeconfig="$ROOT_DIR/.runtime/public-dev/admin-kubernetes.kubeconfig"
  if [[ -f "$default_admin_kubeconfig" ]]; then
    ADMIN_KUBECONFIG="$default_admin_kubeconfig"
  else
    echo "[admin-kubernetes] missing: ADMIN_KUBECONFIG or generated least-privilege kubeconfig"
    missing=1
  fi
fi

if [[ -n "${ADMIN_KUBECONFIG:-}" && ! -f "$ADMIN_KUBECONFIG" ]]; then
  echo "[admin-kubernetes] missing file: ADMIN_KUBECONFIG=$ADMIN_KUBECONFIG"
  missing=1
fi

provider_model="${SEED_MODEL_KEY:-${OPENAI_COMPATIBLE_MODEL:-}}"
provider_endpoint="${SEED_MODEL_API_ENDPOINT:-${OPENAI_COMPATIBLE_BASE_URL:-}}"

if [[ -z "$provider_model" ]]; then
  echo "[provider] missing: OPENAI_COMPATIBLE_MODEL"
  missing=1
fi

if [[ -z "$provider_endpoint" ]]; then
  echo "[provider] missing: OPENAI_COMPATIBLE_BASE_URL"
  missing=1
fi

if [[ -z "${SEED_MODEL_API_KEY_ENCRYPTED:-}" && -z "${MODEL_PROVIDER_TEST_API_KEY:-}" && -z "${OPENAI_COMPATIBLE_API_KEY:-}" ]]; then
  echo "[provider] missing: OPENAI_COMPATIBLE_API_KEY"
  missing=1
fi

if [[ "$missing" -ne 0 ]]; then
  echo "Gate configuration: incomplete"
  exit 2
fi

echo "Gate configuration: complete"
