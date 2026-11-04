#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_ENV_FILE="$ROOT_DIR/backend/.env"
UNIT_NAME="${ENTERPRISE_KNOWLEDGE_DATA_SERVICE_UNIT:-hermes-enterprise-knowledge-data.service}"

usage() {
  echo "Usage: $0 [--backend-env <path>]" >&2
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --backend-env)
      [[ "$#" -ge 2 ]] || { usage; exit 2; }
      BACKEND_ENV_FILE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

[[ -f "$BACKEND_ENV_FILE" ]] || {
  echo "enterprise data check: backend environment is missing: $BACKEND_ENV_FILE" >&2
  exit 1
}

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$ROOT_DIR/scripts/lib/no-proxy-env.sh"
set -a
# shellcheck disable=SC1090
source "$BACKEND_ENV_FILE"
set +a
hermes_clear_proxy_env

base_url="${KNOWLEDGE_DATA_SERVICE_BASE_URL:-}"
[[ -n "$base_url" ]] || {
  echo "enterprise data check: KNOWLEDGE_DATA_SERVICE_BASE_URL is missing" >&2
  exit 1
}
[[ -n "${DATA_SERVICE_ASSERTION_SECRET:-}" ]] || {
  echo "enterprise data check: DATA_SERVICE_ASSERTION_SECRET is missing" >&2
  exit 1
}
[[ -n "${DATA_SERVICE_ASSERTION_AUDIENCE:-}" ]] || {
  echo "enterprise data check: DATA_SERVICE_ASSERTION_AUDIENCE is missing" >&2
  exit 1
}

python3 - "$base_url" <<'PY'
from urllib.parse import urlparse
import sys

parsed = urlparse(sys.argv[1])
if (
    parsed.scheme != "http"
    or parsed.hostname not in {"127.0.0.1", "localhost"}
    or parsed.port is None
    or parsed.path not in {"", "/"}
    or parsed.query
    or parsed.fragment
):
    raise SystemExit(
        "enterprise data check: KNOWLEDGE_DATA_SERVICE_BASE_URL must be a local http://127.0.0.1:<port> address"
    )
PY

systemctl is-active --quiet "$UNIT_NAME" || {
  echo "enterprise data check: systemd unit is not active: $UNIT_NAME" >&2
  exit 1
}

wait_for_endpoint() {
  local endpoint="$1"
  local url="${base_url%/}${endpoint}"
  for _ in $(seq 1 30); do
    if curl --noproxy '*' -fsS --max-time 5 "$url" >/dev/null; then
      return 0
    fi
    sleep 0.5
  done
  echo "enterprise data check: endpoint is not ready: $url" >&2
  return 1
}

for endpoint in /health/live /health/ready; do
  wait_for_endpoint "$endpoint"
done

[[ -x "$ROOT_DIR/backend/.venv/bin/python" ]] || {
  echo "enterprise data check: backend virtual environment is missing" >&2
  exit 1
}

PYTHONPATH="$ROOT_DIR/backend" "$ROOT_DIR/backend/.venv/bin/python" -c '
import asyncio
from app.services.knowledge_assertion import KnowledgeAssertionIssuer, KnowledgeIdentity
from app.services.runtime_knowledge import RuntimeKnowledgeAccess, RuntimeKnowledgeService


class ProbeStore:
    async def authenticate(self, *_args):
        return RuntimeKnowledgeAccess(
            identity=KnowledgeIdentity(
                org_id=1,
                user_id=1,
                user_code="enterprise-data-health-probe",
                runtime_workspace_id=1,
            ),
            enabled=True,
        )


async def main():
    from app.config import settings
    service = RuntimeKnowledgeService(
        ProbeStore(),
        issuer=KnowledgeAssertionIssuer(
            issuer=settings.KNOWLEDGE_ASSERTION_ISSUER,
            ttl_seconds=settings.KNOWLEDGE_ASSERTION_TTL_SECONDS,
        ),
    )
    response = await service.sql_context(
        1,
        None,
        "enterprise-data-health-probe",
        {"query": "企业知识服务健康检查"},
    )
    required = {"schema", "guidance", "examples", "sql_policy", "table_capabilities"}
    if required.difference(response):
        raise RuntimeError("SQL context probe response is incomplete")


asyncio.run(main())
'

echo "enterprise data check: ready ($UNIT_NAME, ${base_url%/})"
