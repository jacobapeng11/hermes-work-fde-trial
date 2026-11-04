#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

run_local_contracts() {
  (
    cd "$ROOT_DIR/backend"
    .venv/bin/pytest \
      tests/unit/test_schema_bootstrap.py \
      tests/unit/personal_knowledge/ \
      tests/unit/test_personal_knowledge_smoke_scripts.py \
      tests/unit/test_personal_knowledge_models.py \
      tests/unit/test_personal_knowledge_schemas.py \
      tests/unit/test_personal_knowledge_provider.py \
      tests/unit/test_personal_knowledge_oss.py \
      tests/unit/test_personal_knowledge_acl.py \
      tests/unit/test_personal_knowledge_api.py \
      tests/unit/test_runtime_personal_knowledge_service.py \
      tests/unit/test_runtime_personal_knowledge_api.py \
      -q
  )
  (
    cd "$ROOT_DIR/runtime/personal-knowledge-plugin"
    PYTHONPATH=src "$ROOT_DIR/backend/.venv/bin/pytest" tests -q
  )
  (
    cd "$ROOT_DIR/frontend/user-web"
    npm test -- \
      src/components/knowledge/KnowledgePage.test.tsx \
      src/components/knowledge/PersonalKnowledgePanel.test.tsx
    npm run lint
  )
}

if [[ "${PERSONAL_KNOWLEDGE_SMOKE_REAL:-0}" != "1" ]]; then
  run_local_contracts
  echo "personal knowledge contract smoke: passed"
  exit 0
fi

: "${PERSONAL_KNOWLEDGE_SMOKE_API_BASE:?required}"
: "${PERSONAL_KNOWLEDGE_SMOKE_USER_TOKEN:?required}"
: "${PERSONAL_KNOWLEDGE_SMOKE_RUNTIME_ID:?required}"
: "${PERSONAL_KNOWLEDGE_SMOKE_RUNTIME_TOKEN:?required}"
: "${PERSONAL_KNOWLEDGE_SMOKE_FIXTURE:?required}"

if [[ ! -f "$PERSONAL_KNOWLEDGE_SMOKE_FIXTURE" ]]; then
  echo "personal knowledge real smoke: fixture not found" >&2
  exit 2
fi

API_BASE="${PERSONAL_KNOWLEDGE_SMOKE_API_BASE%/}"
SMOKE_PROVIDER="${PERSONAL_KNOWLEDGE_SMOKE_PROVIDER:-filebase}"
case "$SMOKE_PROVIDER" in
  filebase|weknora) ;;
  *)
    echo "personal knowledge real smoke: unsupported provider=$SMOKE_PROVIDER" >&2
    exit 2
    ;;
esac
USER_HEADER="Authorization: Bearer $PERSONAL_KNOWLEDGE_SMOKE_USER_TOKEN"
RUNTIME_HEADER="Authorization: Bearer $PERSONAL_KNOWLEDGE_SMOKE_RUNTIME_TOKEN"
KB_ID=""
SMOKE_COMPLETE=0

json_field() {
  local field="$1"
  python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$field"
}

cleanup() {
  local exit_status=$?
  trap - EXIT
  if [[ "$SMOKE_COMPLETE" != "1" && -n "$KB_ID" ]]; then
    local cleanup_status
    cleanup_status="$(
      curl -sS \
        -o /dev/null \
        -w '%{http_code}' \
        -X DELETE \
        -H "$USER_HEADER" \
        "$API_BASE/user/personal-knowledge/$KB_ID"
    )" || cleanup_status="000"
    if [[ "$cleanup_status" != "204" && "$cleanup_status" != "404" ]]; then
      echo "personal knowledge real smoke: cleanup residue: knowledge_base_id=$KB_ID" >&2
    fi
  fi
  exit "$exit_status"
}
trap cleanup EXIT

create_response="$(
  curl -fsS \
    -X POST \
    -H "$USER_HEADER" \
    -H "Content-Type: application/json" \
    --data '{"name":"个人知识库真实链路 Smoke","description":"自动创建并自动清理"}' \
    "$API_BASE/user/personal-knowledge"
)"
KB_ID="$(json_field knowledge_base_id <<<"$create_response")"

upload_response="$(
  curl -fsS \
    -X POST \
    -H "$USER_HEADER" \
    -F "file=@$PERSONAL_KNOWLEDGE_SMOKE_FIXTURE" \
    "$API_BASE/user/personal-knowledge/$KB_ID/files"
)"
initial_status="$(json_field status <<<"$upload_response")"
if [[ "$initial_status" != "pending" ]]; then
  echo "personal knowledge real smoke: expected initial pending status" >&2
  exit 1
fi

deadline=$((SECONDS + ${PERSONAL_KNOWLEDGE_SMOKE_READY_TIMEOUT_SECONDS:-300}))
ready=0
while (( SECONDS < deadline )); do
  files_response="$(
    curl -fsS \
      -H "$USER_HEADER" \
      "$API_BASE/user/personal-knowledge/$KB_ID/files"
  )"
  if python3 -c 'import json,sys; raise SystemExit(0 if any(item.get("status") == "ready" for item in json.load(sys.stdin).get("items", [])) else 1)' <<<"$files_response"; then
    ready=1
    break
  fi
  sleep 5
done

if [[ "$ready" != "1" ]]; then
  echo "personal knowledge real smoke: external_async_ingest_contract_unconfirmed" >&2
  exit 1
fi

curl -fsS \
  -X PUT \
  -H "$USER_HEADER" \
  -H "Content-Type: application/json" \
  --data '{"enabled":true}' \
  "$API_BASE/user/runtime-workspace/$PERSONAL_KNOWLEDGE_SMOKE_RUNTIME_ID/personal-knowledge/$KB_ID" \
  >/dev/null

enabled_state_response="$(
  curl -fsS \
    -H "$RUNTIME_HEADER" \
    "$API_BASE/runtime-personal-knowledge/$PERSONAL_KNOWLEDGE_SMOKE_RUNTIME_ID/state"
)"
if ! python3 -c 'import json,sys; target=int(sys.argv[1]); payload=json.load(sys.stdin); raise SystemExit(0 if payload.get("enabled") and any(item.get("id") == target for item in payload.get("knowledge_bases", [])) else 1)' "$KB_ID" <<<"$enabled_state_response"; then
  echo "personal knowledge real smoke: enabled knowledge base missing from runtime state" >&2
  exit 1
fi

search_response="$(
  curl -fsS \
    -X POST \
    -H "$RUNTIME_HEADER" \
    -H "Content-Type: application/json" \
    --data "{\"query\":\"个人知识库真实链路 Smoke\",\"knowledge_base_ids\":[$KB_ID]}" \
    "$API_BASE/runtime-personal-knowledge/$PERSONAL_KNOWLEDGE_SMOKE_RUNTIME_ID/search"
)"

if ! python3 -c 'import json,sys; matches=json.load(sys.stdin).get("matches", []); first=matches[0] if matches else {}; raise SystemExit(0 if isinstance(first.get("file_id"), int) and "&file=" in str(first.get("source_url") or "") else 1)' <<<"$search_response"; then
  echo "personal knowledge real smoke: search returned no platform file citation" >&2
  exit 1
fi

if [[ "$SMOKE_PROVIDER" == "weknora" ]]; then
  if ! python3 -c 'import json,sys; target=int(sys.argv[1]); payload=json.load(sys.stdin); caps=next((item.get("capabilities") or {} for item in payload.get("knowledge_bases", []) if item.get("id") == target), {}); raise SystemExit(0 if caps.get("rag") is True and caps.get("wiki") is True else 1)' "$KB_ID" <<<"$enabled_state_response"; then
    echo "personal knowledge real smoke: expected RAG and Wiki capabilities" >&2
    exit 1
  fi

  wiki_deadline=$((SECONDS + ${PERSONAL_KNOWLEDGE_SMOKE_WIKI_TIMEOUT_SECONDS:-300}))
  wiki_matches=0
  while (( SECONDS < wiki_deadline )); do
    wiki_search_response="$(
      curl -fsS \
        -X POST \
        -H "$RUNTIME_HEADER" \
        -H "Content-Type: application/json" \
        --data "{\"query\":\"个人知识库真实链路 Smoke\",\"knowledge_base_ids\":[$KB_ID]}" \
        "$API_BASE/runtime-personal-knowledge/$PERSONAL_KNOWLEDGE_SMOKE_RUNTIME_ID/wiki-search"
    )"
    if python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("matches") else 1)' <<<"$wiki_search_response"; then
      wiki_matches=1
      break
    fi
    sleep 5
  done
  if [[ "$wiki_matches" != "1" ]]; then
    echo "personal knowledge real smoke: wiki search returned no matches" >&2
    exit 1
  fi

  wiki_slug="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["matches"][0]["slug"])' <<<"$wiki_search_response")"
  read_payload="$(python3 -c 'import json,sys; print(json.dumps({"knowledge_base_id":int(sys.argv[1]),"slug":sys.argv[2]},ensure_ascii=False))' "$KB_ID" "$wiki_slug")"
  wiki_page_response="$(
    curl -fsS \
      -X POST \
      -H "$RUNTIME_HEADER" \
      -H "Content-Type: application/json" \
      --data "$read_payload" \
      "$API_BASE/runtime-personal-knowledge/$PERSONAL_KNOWLEDGE_SMOKE_RUNTIME_ID/wiki-read-page"
  )"
  if ! python3 -c 'import json,sys; payload=json.load(sys.stdin); files=payload.get("source_files") or []; raise SystemExit(0 if payload.get("content") and any(isinstance(item.get("file_id"), int) and "&file=" in str(item.get("source_url") or "") for item in files) else 1)' <<<"$wiki_page_response"; then
    echo "personal knowledge real smoke: wiki page missing content or platform sources" >&2
    exit 1
  fi
fi

curl -fsS \
  -X PUT \
  -H "$USER_HEADER" \
  -H "Content-Type: application/json" \
  --data '{"enabled":false}' \
  "$API_BASE/user/runtime-workspace/$PERSONAL_KNOWLEDGE_SMOKE_RUNTIME_ID/personal-knowledge/$KB_ID" \
  >/dev/null

disabled_state_response="$(
  curl -fsS \
    -H "$RUNTIME_HEADER" \
    "$API_BASE/runtime-personal-knowledge/$PERSONAL_KNOWLEDGE_SMOKE_RUNTIME_ID/state"
)"
if ! python3 -c 'import json,sys; target=int(sys.argv[1]); payload=json.load(sys.stdin); raise SystemExit(0 if all(item.get("id") != target for item in payload.get("knowledge_bases", [])) else 1)' "$KB_ID" <<<"$disabled_state_response"; then
  echo "personal knowledge real smoke: disabled knowledge base remains in runtime state" >&2
  exit 1
fi

revoked_search_status="$(
  curl -sS \
    -o /dev/null \
    -w '%{http_code}' \
    -X POST \
    -H "$RUNTIME_HEADER" \
    -H "Content-Type: application/json" \
    --data "{\"query\":\"个人知识库真实链路 Smoke\",\"knowledge_base_ids\":[$KB_ID]}" \
    "$API_BASE/runtime-personal-knowledge/$PERSONAL_KNOWLEDGE_SMOKE_RUNTIME_ID/search"
)"
if [[ "$revoked_search_status" != "403" ]]; then
  echo "personal knowledge real smoke: disabled knowledge base search was not rejected" >&2
  exit 1
fi

curl -fsS \
  -X DELETE \
  -H "$USER_HEADER" \
  "$API_BASE/user/personal-knowledge/$KB_ID" \
  >/dev/null

deleted_status="$(
  curl -sS \
    -o /dev/null \
    -w '%{http_code}' \
    -H "$USER_HEADER" \
    "$API_BASE/user/personal-knowledge/$KB_ID"
)"
if [[ "$deleted_status" != "404" ]]; then
  echo "personal knowledge real smoke: expected deleted knowledge base to return 404" >&2
  exit 1
fi

KB_ID=""
SMOKE_COMPLETE=1
trap - EXIT

echo "personal knowledge real smoke: passed"
