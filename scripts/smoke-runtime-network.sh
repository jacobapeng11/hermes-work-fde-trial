#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/backend/.env"
OVERRIDE_ROUTE_HOSTS="${HERMES_RUNTIME_TEST_ROUTE_HOSTS:-}"
OVERRIDE_ROUTE_HOST="${HERMES_RUNTIME_TEST_ROUTE_HOST:-}"
OVERRIDE_INGRESS_BASE_URL="${HERMES_RUNTIME_INGRESS_BASE_URL:-}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Gate 类型: environment" >&2
  echo "阻塞项: backend/.env 不存在。" >&2
  echo "影响范围: 阻塞 Profile 4 runtime network smoke。" >&2
  echo "验证命令: bash scripts/check-env.sh" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
hermes_clear_proxy_env

if [[ -n "$OVERRIDE_ROUTE_HOSTS" ]]; then
  HERMES_RUNTIME_TEST_ROUTE_HOSTS="$OVERRIDE_ROUTE_HOSTS"
elif [[ -n "$OVERRIDE_ROUTE_HOST" ]]; then
  HERMES_RUNTIME_TEST_ROUTE_HOST="$OVERRIDE_ROUTE_HOST"
fi
if [[ -n "$OVERRIDE_INGRESS_BASE_URL" ]]; then
  HERMES_RUNTIME_INGRESS_BASE_URL="$OVERRIDE_INGRESS_BASE_URL"
fi

ROUTE_HOSTS="${HERMES_RUNTIME_TEST_ROUTE_HOSTS:-${HERMES_RUNTIME_TEST_ROUTE_HOST:-}}"
INGRESS_BASE_URL="${HERMES_RUNTIME_INGRESS_BASE_URL:-}"

if [[ -z "$ROUTE_HOSTS" ]]; then
  echo "Gate 类型: k8s-knative" >&2
  echo "阻塞项: 缺少 HERMES_RUNTIME_TEST_ROUTE_HOST 或 HERMES_RUNTIME_TEST_ROUTE_HOSTS。" >&2
  echo "影响范围: 无法验证 Hermes Dashboard /api/status。" >&2
  echo "建议配置: 提供测试 KService status.url 的 host。" >&2
  echo "验证命令: curl -sf -u platform:<api-key> -H \"Host: <route-host>\" <ingress-base-url>/api/status" >&2
  exit 2
fi

if [[ -z "$INGRESS_BASE_URL" ]]; then
  echo "Gate 类型: k8s-knative" >&2
  echo "阻塞项: 缺少 HERMES_RUNTIME_INGRESS_BASE_URL。" >&2
  echo "影响范围: 无法验证 Kourier NodePort + Host Header 到 Hermes Dashboard。" >&2
  echo "建议配置: 提供 backend 宿主机可访问的 Knative ingress base URL。" >&2
  exit 2
fi

IFS=',' read -r -a route_hosts <<< "$ROUTE_HOSTS"
verified=0

for route_host in "${route_hosts[@]}"; do
  route_host="$(printf '%s' "$route_host" | xargs)"
  [[ -z "$route_host" ]] && continue

  if [[ "$route_host" == *.svc.cluster.local ]]; then
    echo "Gate 类型: k8s-knative" >&2
    echo "阻塞项: route host 仍是 cluster-local host，不能作为集群外 NodePort + Host Header 验收: $route_host" >&2
    echo "影响范围: Backend 在 K8s 外时无法按正式设计验证 runtime Dashboard。" >&2
    echo "建议配置: 为 Knative config-domain 设置外部 Host Header 域，例如 hermes.local，然后使用 <ksvc>.<namespace>.hermes.local。" >&2
    exit 2
  fi

  IFS='.' read -r service_name namespace_name _ <<< "$route_host"
  secret_ref=""
  if command -v kubectl >/dev/null 2>&1 && [[ -n "$service_name" && -n "$namespace_name" ]]; then
    secret_ref="$(
      kubectl get ksvc "$service_name" -n "$namespace_name" \
        -o jsonpath='{range .spec.template.spec.containers[0].env[?(@.name=="API_SERVER_KEY")]}{.valueFrom.secretKeyRef.name}{" "}{.valueFrom.secretKeyRef.key}{end}' 2>/dev/null || true
    )"
  fi
  read -r api_secret_name api_secret_key <<< "$secret_ref"

  auth_args=()
  if [[ -n "${api_secret_name:-}" && -n "${api_secret_key:-}" ]]; then
    api_server_key="$(kubectl get secret "$api_secret_name" -n "$namespace_name" -o "jsonpath={.data.$api_secret_key}" | base64 -d)"
    auth_args=(-u "platform:$api_server_key")
  fi

  curl -sf -H "Host: $route_host" "${auth_args[@]}" "$INGRESS_BASE_URL/api/status"
  echo "runtime network smoke: ok host=$route_host"
  verified=$((verified + 1))
done

if [[ "$verified" -eq 0 ]]; then
  echo "Gate 类型: k8s-knative" >&2
  echo "阻塞项: 没有可验证的 route host。" >&2
  exit 2
fi
