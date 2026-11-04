#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

USER_WEB_FORBIDDEN_PATTERN='BusinessWorkspace|copaw_|provider[[:space:]_-]*key|api[[:space:]_-]*key|runtime[[:space:]_-]*token|host[[:space:]_-]*path|kservice|kubeconfig'
SCHEMA_FORBIDDEN_PATTERN='BusinessWorkspace|copaw_|provider[[:space:]_-]*key|api[[:space:]_-]*key|runtime[[:space:]_-]*token|host[[:space:]_-]*path'

require_visual_contract() {
  local pattern="$1"
  local description="$2"
  if ! rg -q --fixed-strings -- "$pattern" "frontend/user-web/src/styles.css"; then
    echo "user-web contract check: visual layout contract missing: $description" >&2
    exit 1
  fi
}

require_source_contract() {
  local pattern="$1"
  local path="$2"
  local description="$3"
  if ! rg -q --fixed-strings "$pattern" "$path"; then
    echo "user-web contract check: source contract missing: $description" >&2
    exit 1
  fi
}

forbid_source_contract() {
  local pattern="$1"
  local path="$2"
  local description="$3"
  if rg -q --fixed-strings "$pattern" "$path"; then
    echo "user-web contract check: retired source contract returned: $description" >&2
    exit 1
  fi
}

if rg -n -i "$USER_WEB_FORBIDDEN_PATTERN" "frontend/user-web/src"; then
  echo "user-web contract check: forbidden platform-internal or legacy term found in user-web source" >&2
  exit 1
fi

if rg -n -i "$SCHEMA_FORBIDDEN_PATTERN" "packages/schemas/src"; then
  echo "user-web contract check: forbidden platform-internal or legacy term found" >&2
  exit 1
fi

require_visual_contract '--sidebar-width: 252px;' 'desktop sidebar width token'
require_visual_contract 'grid-template-columns: var(--sidebar-width) minmax(0, 1fr);' 'desktop sidebar layout'
require_visual_contract 'grid-template-rows: 64px minmax(0, 1fr);' 'desktop header height'
require_visual_contract 'width: 324px;' 'desktop file rail width'
require_visual_contract '.filesPane { display: none; }' 'mobile file rail collapse'
require_visual_contract 'width: min(410px, 100vw);' 'capability drawer width'
require_visual_contract 'margin: 0 auto 24px;' 'desktop composer anchor'
require_visual_contract '.messageFeedInner[data-has-messages="true"] { justify-content: flex-start; }' 'top-aligned populated conversation'
require_visual_contract '.message[data-role="assistant"] { width: min(760px, 100%); padding: 4px 0 8px; border-radius: 0; background: transparent; }' 'flat assistant response surface'
require_visual_contract '@media (max-width: 760px)' 'mobile responsive breakpoint'
require_visual_contract 'max-height: 320px;' 'bounded tool detail height'
require_source_contract 'chat.reasoning.delta' 'backend/app/api/v1/user/chat.py' 'backend reasoning activity event'
require_source_contract 'chat.reasoning.delta' 'frontend/user-web/src/pages/chatRunProjection.ts' 'frontend reasoning projection'
require_source_contract '思考中…' 'frontend/user-web/src/components/ExecutionTrace.tsx' 'live reasoning disclosure'
require_source_contract '思考过程' 'frontend/user-web/src/components/ExecutionTrace.tsx' 'completed reasoning disclosure'
require_source_contract 'LoaderCircle className="reasoningSpinner reasoningIcon"' 'frontend/user-web/src/components/ChatMessageList.tsx' 'single lightweight activity spinner'
forbid_source_contract 'loadingo.gif' 'frontend/user-web/src/components/ChatMessageList.tsx' 'Hermes GIF activity asset'
forbid_source_contract 'agentActivityAvatar' 'frontend/user-web/src/components/ChatMessageList.tsx' 'activity avatar lane'
forbid_source_contract 'assistantStreamingIndicator' 'frontend/user-web/src/components/ChatMessageList.tsx' 'duplicate answer-tail spinner'
require_source_contract 'reasoningContent' 'frontend/user-web/src/pages/chatHistory.ts' 'reasoning history restoration'
require_source_contract 'ToolActivityItem' 'frontend/user-web/src/components/ExecutionTrace.tsx' 'grouped user-facing tool activity'
require_source_contract 'aria-live="polite"' 'frontend/user-web/src/components/ToolActivityGroup.tsx' 'accessible tool status updates'
require_source_contract '正在执行操作…' 'frontend/user-web/src/toolPresentation.ts' 'generic user-facing tool fallback'
require_source_contract 'export interface ScheduledTask' 'packages/schemas/src/index.ts' 'scheduled task DTO'
require_source_contract 'scheduled-tasks' 'frontend/user-web/src/api/client.ts' 'scheduled task API path'
require_source_contract 'scheduledTaskCommandPayload' 'frontend/user-web/src/api/client.ts' 'scheduled task command field allowlist'
require_source_contract 'sends only the command fields' 'frontend/user-web/src/api/client.test.ts' 'derived scheduled task fields excluded from command payload'
require_source_contract 'disabled={busy || !sessionAvailable}' 'frontend/user-web/src/components/schedules/ScheduledTaskCard.tsx' 'unavailable target task trigger disabled'

echo "user-web contract check: ok"
