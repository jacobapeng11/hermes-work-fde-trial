#!/usr/bin/env bash
# End-to-end smoke for the public scheduled-task API.
#
# This script only removes resources whose IDs were returned by its own create
# requests.  It never searches by label or deletes pre-existing data.
# Optional Kubernetes/Knative hooks in this script operate on Runtime Pods only.
# The host Backend Scanner is checked separately by
# check-scheduled-task-deployment.sh and is never scaled or restarted here.
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

fail() {
  echo "scheduled-task smoke: $*" >&2
  exit 2
}

require_value() {
  local name="$1"
  local value="${!name:-}"
  [[ -n "${value//[[:space:]]/}" ]] || fail "missing required environment variable: $name"
}

require_positive_integer() {
  local name="$1"
  local value="$2"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || fail "$name must be a positive integer"
}

# Safety gate: all required input is checked before a trap is installed or a
# network client is invoked.  In particular, a typo cannot trigger cleanup.
require_value SMOKE_BASE_URL
require_value SMOKE_TOKEN
require_value SMOKE_WORKSPACE_ID
require_value SMOKE_RUN_LABEL

SMOKE_BASE_URL="${SMOKE_BASE_URL%/}"
SMOKE_TOKEN="${SMOKE_TOKEN}"
SMOKE_WORKSPACE_ID="${SMOKE_WORKSPACE_ID}"
SMOKE_RUN_LABEL="${SMOKE_RUN_LABEL}"
SMOKE_TIMEOUT_SECONDS="${SMOKE_TIMEOUT_SECONDS:-180}"
SMOKE_POLL_SECONDS="${SMOKE_POLL_SECONDS:-3}"
SMOKE_DELAY_SECONDS="${SMOKE_DELAY_SECONDS:-30}"
SMOKE_DRY_RUN="${SMOKE_DRY_RUN:-0}"

[[ "$SMOKE_BASE_URL" =~ ^https?://[^[:space:]]+$ ]] || fail "SMOKE_BASE_URL must be an http(s) URL"
[[ "$SMOKE_WORKSPACE_ID" =~ ^[1-9][0-9]*$ ]] || fail "SMOKE_WORKSPACE_ID must be a positive integer"
# The label is intentionally supplied by the operator.  It is embedded in
# every created name, so operators can make a run traceable without exposing a
# prompt or token in output.  Eight characters avoids accidental common names.
[[ "$SMOKE_RUN_LABEL" =~ ^[A-Za-z0-9][A-Za-z0-9-]{7,63}$ ]] || \
  fail "SMOKE_RUN_LABEL must be a unique 8-64 character alphanumeric/hyphen label"
require_positive_integer SMOKE_TIMEOUT_SECONDS "$SMOKE_TIMEOUT_SECONDS"
require_positive_integer SMOKE_POLL_SECONDS "$SMOKE_POLL_SECONDS"
require_positive_integer SMOKE_DELAY_SECONDS "$SMOKE_DELAY_SECONDS"
[[ "$SMOKE_DRY_RUN" == "0" || "$SMOKE_DRY_RUN" == "1" ]] || fail "SMOKE_DRY_RUN must be 0 or 1"
(( SMOKE_DELAY_SECONDS < SMOKE_TIMEOUT_SECONDS )) || fail "SMOKE_DELAY_SECONDS must be less than SMOKE_TIMEOUT_SECONDS"

if [[ "$SMOKE_BASE_URL" == */api/v1 ]]; then
  API_BASE="$SMOKE_BASE_URL"
else
  API_BASE="$SMOKE_BASE_URL/api/v1"
fi

if [[ "$SMOKE_DRY_RUN" == "1" ]]; then
  echo "scheduled-task smoke: dry-run validation passed label=$SMOKE_RUN_LABEL workspace_id=$SMOKE_WORKSPACE_ID"
  exit 0
fi

command -v curl >/dev/null || fail "curl is required"
command -v jq >/dev/null || fail "jq is required"
command -v date >/dev/null || fail "date is required"

TMP_DIR="$(mktemp -d)"
SESSION_ID=""
declare -a TASK_IDS=()
ZERO_SCALE_ACTIVE=0
ZERO_SCALE_RESTORE_HOOK="${SMOKE_ZERO_SCALE_RESTORE_HOOK:-}"
FAILURE_ACTIVE=0
FAILURE_RESTORE_HOOK="${SMOKE_FAILURE_RESTORE_HOOK:-}"

api_request() {
  local method="$1"
  local path="$2"
  local expected_status="$3"
  local payload="${4:-}"
  local response_file="$TMP_DIR/response.json"
  local response_status
  local -a curl_args=(
    --silent --show-error --location
    --connect-timeout 10 --max-time 45
    --output "$response_file" --write-out '%{http_code}'
    --request "$method"
    --header "Authorization: Bearer $SMOKE_TOKEN"
    --header 'Accept: application/json'
  )
  if [[ -n "$payload" ]]; then
    curl_args+=(--header 'Content-Type: application/json' --data "$payload")
  fi
  if ! response_status="$(curl "${curl_args[@]}" "$API_BASE$path")"; then
    fail "request failed: $method $path"
  fi
  if [[ ",$expected_status," != *",$response_status,"* ]]; then
    local response_excerpt
    response_excerpt="$(head -c 500 "$response_file" | tr '\n' ' ')"
    fail "unexpected HTTP $response_status for $method $path: $response_excerpt"
  fi
  cat "$response_file"
}

run_optional_hook() {
  local hook="$1"
  local description="$2"
  [[ -x "$hook" ]] || fail "$description hook must be an executable file"
  SMOKE_BASE_URL="$SMOKE_BASE_URL" \
  SMOKE_WORKSPACE_ID="$SMOKE_WORKSPACE_ID" \
  SMOKE_RUN_LABEL="$SMOKE_RUN_LABEL" \
    "$hook"
}

cleanup() {
  local exit_status=$?
  set +e
  local cleanup_delete
  cleanup_delete() {
    local path="$1"
    local resource="$2"
    local response_status
    response_status="$(curl --silent --show-error --location --connect-timeout 10 --max-time 45 \
      --output /dev/null --write-out '%{http_code}' --request DELETE \
      --header "Authorization: Bearer $SMOKE_TOKEN" "$API_BASE$path")"
    if [[ "$response_status" == "204" || "$response_status" == "404" ]]; then
      echo "scheduled-task smoke: cleaned $resource" >&2
    else
      echo "scheduled-task smoke: cleanup failed for $resource (HTTP $response_status)" >&2
    fi
  }
  # Restore external drill state before deleting a Session.  Session deletion
  # may contact Runtime in real mode, so cleanup must not depend on a disabled
  # endpoint or an intentionally scaled-to-zero service.
  if [[ "$FAILURE_ACTIVE" == "1" && -n "$FAILURE_RESTORE_HOOK" ]]; then
    run_optional_hook "$FAILURE_RESTORE_HOOK" "failure restore" \
      || echo "scheduled-task smoke: failure restore hook failed" >&2
  fi
  if [[ "$ZERO_SCALE_ACTIVE" == "1" && -n "$ZERO_SCALE_RESTORE_HOOK" ]]; then
    run_optional_hook "$ZERO_SCALE_RESTORE_HOOK" "zero-scale restore" \
      || echo "scheduled-task smoke: zero-scale restore hook failed" >&2
  fi
  local index
  for ((index=${#TASK_IDS[@]} - 1; index >= 0; index -= 1)); do
    cleanup_delete "/user/runtime-workspace/$SMOKE_WORKSPACE_ID/scheduled-tasks/${TASK_IDS[$index]}" "task_id=${TASK_IDS[$index]}"
  done
  if [[ -n "$SESSION_ID" ]]; then
    cleanup_delete "/user/runtime-workspace/$SMOKE_WORKSPACE_ID/sessions/$SESSION_ID" "session_id=$SESSION_ID"
  fi
  rm -rf "$TMP_DIR"
  exit "$exit_status"
}
trap cleanup EXIT

require_staging_drill_authorization() {
  [[ "${SMOKE_STAGING_AUTHORIZED:-0}" == "1" ]] || \
    fail "staging drills require SMOKE_STAGING_AUTHORIZED=1"
  [[ "$SMOKE_BASE_URL" =~ (^|[./-])(staging|stage|stg)([./-]|$) ]] || \
    fail "staging drills require a staging-looking SMOKE_BASE_URL"
}

maybe_run_zero_scale_drill() {
  [[ "${SMOKE_ENABLE_ZERO_SCALE_DRILL:-0}" == "1" ]] || return 0
  require_staging_drill_authorization
  local hook="${SMOKE_ZERO_SCALE_HOOK:-}"
  [[ -n "$hook" && -n "$ZERO_SCALE_RESTORE_HOOK" ]] || \
    fail "zero-scale drill requires SMOKE_ZERO_SCALE_HOOK and SMOKE_ZERO_SCALE_RESTORE_HOOK"
  run_optional_hook "$hook" "zero-scale"
  ZERO_SCALE_ACTIVE=1
  echo "scheduled-task smoke: zero-scale hook completed; next scheduled run must wake Runtime" >&2
}

maybe_run_failure_drill() {
  [[ "${SMOKE_ENABLE_FAILURE_DRILL:-0}" == "1" ]] || return 0
  require_staging_drill_authorization
  local prepare_hook="${SMOKE_FAILURE_PREPARE_HOOK:-}"
  local restore_hook="$FAILURE_RESTORE_HOOK"
  [[ -n "$prepare_hook" && -n "$restore_hook" ]] || \
    fail "failure drill requires SMOKE_FAILURE_PREPARE_HOOK and SMOKE_FAILURE_RESTORE_HOOK"
  run_optional_hook "$prepare_hook" "failure prepare"
  FAILURE_ACTIVE=1
  local failure_task_id
  failure_task_id="$(create_once_task "failure")"
  TASK_IDS+=("$failure_task_id")
  wait_for_task_failure "$failure_task_id"
  run_optional_hook "$restore_hook" "failure restore"
  FAILURE_ACTIVE=0
  echo "scheduled-task smoke: failure drill passed task_id=$failure_task_id" >&2
}

deadline_epoch=$(( $(date -u +%s) + SMOKE_TIMEOUT_SECONDS ))

wait_for_task_success() {
  local task_id="$1"
  local task_json
  while (( $(date -u +%s) < deadline_epoch )); do
    task_json="$(api_request GET "/user/runtime-workspace/$SMOKE_WORKSPACE_ID/scheduled-tasks/$task_id" "200")"
    if jq -e '.status == "completed" and .last_run_status == "success" and (.last_runtime_run_id | type == "string" and length > 0)' \
      <<<"$task_json" >/dev/null; then
      return 0
    fi
    sleep "$SMOKE_POLL_SECONDS"
  done
  fail "task_id=$task_id did not complete successfully before timeout"
}

wait_for_task_failure() {
  local task_id="$1"
  local task_json
  while (( $(date -u +%s) < deadline_epoch )); do
    task_json="$(api_request GET "/user/runtime-workspace/$SMOKE_WORKSPACE_ID/scheduled-tasks/$task_id" "200")"
    if jq -e '.status == "paused" and .last_run_status == "failed" and (.last_error_code | type == "string" and length > 0)' \
      <<<"$task_json" >/dev/null; then
      return 0
    fi
    sleep "$SMOKE_POLL_SECONDS"
  done
  fail "failure task_id=$task_id did not record a controlled failure before timeout"
}

create_once_task() {
  local suffix="$1"
  local run_at
  run_at="$(date -u -d "+$SMOKE_DELAY_SECONDS seconds" '+%Y-%m-%dT%H:%M:%SZ')"
  local task_name="scheduled-smoke-$SMOKE_RUN_LABEL-$suffix"
  local payload
  payload="$(jq -cn \
    --arg task_name "$task_name" \
    --arg prompt "Reply with exactly: scheduled smoke $SMOKE_RUN_LABEL $suffix" \
    --arg runtime_session_id "$SESSION_ID" \
    --arg run_at "$run_at" \
    '{task_name:$task_name,prompt:$prompt,target_runtime_session_id:$runtime_session_id,schedule_config:{type:"once",run_at:$run_at}}')"
  local response
  response="$(api_request POST "/user/runtime-workspace/$SMOKE_WORKSPACE_ID/scheduled-tasks" "201" "$payload")"
  local task_id
  task_id="$(jq -er '.task_id | if type == "number" and . > 0 then tostring else error("missing task_id") end' <<<"$response")" || \
    fail "task creation response did not contain task_id"
  printf '%s\n' "$task_id"
}

session_payload="$(jq -cn --arg title "scheduled-smoke-$SMOKE_RUN_LABEL" '{title:$title,source_type:"smoke"}')"
session_response="$(api_request POST "/user/runtime-workspace/$SMOKE_WORKSPACE_ID/sessions" "200" "$session_payload")"
SESSION_ID="$(jq -er '.runtime_session_id | select(type == "string" and length > 0)' <<<"$session_response")" || \
  fail "session creation response did not contain runtime_session_id"
echo "scheduled-task smoke: created isolated session label=$SMOKE_RUN_LABEL" >&2

maybe_run_zero_scale_drill
maybe_run_failure_drill

TASK_ID="$(create_once_task "success")"
TASK_IDS+=("$TASK_ID")
wait_for_task_success "$TASK_ID"

history_response="$(api_request GET "/user/chat/history?runtime_workspace_id=$SMOKE_WORKSPACE_ID&runtime_session_id=$SESSION_ID" "200")"
jq -e --argjson task_id "$TASK_ID" --arg task_name "scheduled-smoke-$SMOKE_RUN_LABEL-success" '
  ([.messages[]? | select(
    .role == "user"
    and .source.type == "scheduled"
    and .source.scheduled_task_id == $task_id
    and .source.scheduled_task_name == $task_name
    and .source.task_available == true
  )] | length > 0)
  and
  ([.messages[]? | select(
    .role == "assistant"
    and (((.content // .message // .text // "") | tostring | length) > 0)
  )] | length > 0)
' <<<"$history_response" >/dev/null || fail "history is missing scheduled source metadata or assistant reply"

echo "scheduled-task smoke: passed label=$SMOKE_RUN_LABEL task_id=$TASK_ID" >&2
